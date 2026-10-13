"""One database transaction per Telegram update."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from eng_remember.config import Settings
from eng_remember.db.engine import SessionFactory
from eng_remember.services.container import build_services

__all__ = ["DbSessionMiddleware"]


class DbSessionMiddleware(BaseMiddleware):
    """Open a session, hand the handler a wired ``Services`` object, close it.

    The transaction is committed after the handler returns and rolled back on
    any exception, so a half-finished flow never leaves rows behind. Handlers
    that tell the user "saved" should call ``services.commit()`` first — the
    commit here then becomes a no-op — otherwise a commit failure happens after
    the confirmation message has already been sent.
    """

    def __init__(self, session_factory: SessionFactory, settings: Settings) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._session_factory() as session:
            data["services"] = build_services(session, self._settings)
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result
