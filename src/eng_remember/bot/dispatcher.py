"""Dispatcher assembly: middlewares and router order in one place."""

from __future__ import annotations

import structlog
from aiogram import Dispatcher, F, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

from eng_remember.bot import texts
from eng_remember.bot.middlewares.db import DbSessionMiddleware
from eng_remember.bot.middlewares.dedup import DuplicateMessageMiddleware
from eng_remember.bot.middlewares.rate_limit import RateLimitMiddleware
from eng_remember.bot.middlewares.user import UserMiddleware
from eng_remember.bot.routers import (
    capture,
    errors,
    files,
    library,
    practice,
    preferences,
    review,
    start,
)
from eng_remember.config import Settings
from eng_remember.db.engine import SessionFactory
from eng_remember.services.llm.client import LlmTransport

__all__ = ["build_dispatcher"]

log = structlog.get_logger(__name__)


def build_dispatcher(
    settings: Settings,
    session_factory: SessionFactory,
    *,
    llm_transport: LlmTransport | None = None,
) -> Dispatcher:
    """Wire the dispatcher.

    Middlewares are registered on both ``message`` and ``callback_query``, in
    that order: the rate limit and duplicate guard run before a session is
    opened, then the session, then the user lookup.

    The bot serves private chats only. The chat-type filter is registered on the
    dispatcher's observers, which is checked before any middleware runs, so a
    group or channel update never opens a database session.

    FSM storage is in-memory, which is enough for a single process — a pending
    import confirmation is lost on restart, and nothing else depends on it.
    Swapping in a Redis storage is a one-line change here when the bot is scaled
    to more than one worker.
    """
    dispatcher = Dispatcher(storage=MemoryStorage())

    rate_limit_middleware = RateLimitMiddleware(
        messages_per_minute=settings.bot.messages_per_minute,
        callbacks_per_minute=settings.bot.callbacks_per_minute,
    )
    duplicate_middleware = DuplicateMessageMiddleware(
        ttl_seconds=settings.bot.duplicate_message_ttl_seconds,
    )
    session_middleware = DbSessionMiddleware(session_factory, settings, llm_transport=llm_transport)
    user_middleware = UserMiddleware()
    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.middleware(rate_limit_middleware)
        observer.middleware(duplicate_middleware)
        observer.middleware(session_middleware)
        observer.middleware(user_middleware)

    # Filters are checked before middlewares, so a group or channel update is
    # dropped before a session is opened and no stranger gets a user row.
    dispatcher.message.filter(F.chat.type == "private")
    dispatcher.callback_query.filter(F.message.chat.type == "private")

    dispatcher.include_router(errors.build_router())
    dispatcher.include_router(start.build_router())
    dispatcher.include_router(review.build_router())
    dispatcher.include_router(practice.build_router())
    dispatcher.include_router(library.build_router())
    dispatcher.include_router(files.build_router())
    dispatcher.include_router(preferences.build_router())
    # Last: claims any plain text no other router wanted.
    dispatcher.include_router(capture.build_router())
    dispatcher.include_router(_fallback_router())

    return dispatcher


def _fallback_router() -> Router:
    """The last router: a callback nothing else recognised.

    A handler's own observers are checked before its sub-routers, so this has
    to be a router of its own included last rather than a handler on the
    dispatcher. Every stale button is answered here — otherwise the spinner it
    started turns until the client gives up.
    """
    router = Router(name="fallback")

    @router.callback_query()
    async def unknown_callback(query: CallbackQuery) -> None:
        log.warning(
            "unknown_callback",
            user_id=query.from_user.id,
            data=query.data,
        )
        await query.answer(texts.stale_callback(), show_alert=True)

    return router
