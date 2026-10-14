"""Dispatcher assembly: middlewares and router order in one place."""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from eng_remember.bot.middlewares.db import DbSessionMiddleware
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

__all__ = ["build_dispatcher"]


def build_dispatcher(settings: Settings, session_factory: SessionFactory) -> Dispatcher:
    """Wire the dispatcher.

    Middlewares are registered on both ``message`` and ``callback_query``, in
    that order: the session has to exist before the user can be looked up in it.

    FSM storage is in-memory, which is enough for a single process — a pending
    import confirmation is lost on restart, and nothing else depends on it.
    Swapping in a Redis storage is a one-line change here when the bot is scaled
    to more than one worker.
    """
    dispatcher = Dispatcher(storage=MemoryStorage())

    session_middleware = DbSessionMiddleware(session_factory, settings)
    user_middleware = UserMiddleware()
    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.middleware(session_middleware)
        observer.middleware(user_middleware)

    dispatcher.include_router(errors.build_router())
    dispatcher.include_router(start.build_router())
    dispatcher.include_router(review.build_router())
    dispatcher.include_router(practice.build_router())
    dispatcher.include_router(library.build_router())
    dispatcher.include_router(files.build_router())
    dispatcher.include_router(preferences.build_router())
    # Last: claims any plain text no other router wanted.
    dispatcher.include_router(capture.build_router())

    return dispatcher
