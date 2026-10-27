"""Ignore an identical plain-text message repeated within a short window.

Double taps, client retries and impatience should not buy two model calls. The
key is the learner plus whitespace/case-normalised text; commands are exempt so
``/review`` and ``/stats`` keep working when pressed again.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from eng_remember.bot import texts

__all__ = ["DuplicateMessageMiddleware"]

log = structlog.get_logger(__name__)
_PRUNE_AT = 1000


class DuplicateMessageMiddleware(BaseMiddleware):
    def __init__(
        self,
        *,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._seen: dict[tuple[int, str], float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = data.get("event_from_user")
        if (
            self._ttl <= 0
            or from_user is None
            or from_user.is_bot
            or not isinstance(event, Message)
        ):
            return await handler(event, data)

        text = event.text
        if not text or text.startswith("/"):
            return await handler(event, data)

        key = (from_user.id, " ".join(text.split()).casefold())
        now = self._clock()
        self._prune(now)
        seen_at = self._seen.get(key)
        if seen_at is not None and now - seen_at < self._ttl:
            log.info("duplicate_message_ignored", user_id=from_user.id)
            await event.answer(texts.duplicate_message())
            return None
        self._seen.pop(key, None)

        self._seen[key] = now
        try:
            return await handler(event, data)
        except Exception:
            # A failed request did not become a card; let the learner retry the
            # same text immediately instead of treating it as a duplicate.
            self._seen.pop(key, None)
            raise

    def _prune(self, now: float) -> None:
        if len(self._seen) < _PRUNE_AT:
            return
        cutoff = now - self._ttl
        for key, seen_at in list(self._seen.items()):
            if seen_at <= cutoff:
                del self._seen[key]
