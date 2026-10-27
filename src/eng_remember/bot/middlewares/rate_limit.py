"""Per-learner inbound rate limit.

The global dispatcher semaphore bounds how many updates are processed at once,
but one account can still fill it with a burst. This middleware rejects beyond
a rolling per-minute allowance before a database session is opened, so a flood
costs neither a connection nor a model call.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from eng_remember.bot import texts

__all__ = ["RateLimitMiddleware"]

log = structlog.get_logger(__name__)
WINDOW_SECONDS = 60.0


class SlidingWindowLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[tuple[str, int], deque[float]] = {}

    def allow(self, kind: str, user_id: int, *, limit: int) -> bool:
        now = self._clock()
        bucket = self._buckets.setdefault((kind, user_id), deque())
        cutoff = now - WINDOW_SECONDS
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


class RateLimitMiddleware(BaseMiddleware):
    """Reject messages and button presses over the per-learner allowance."""

    def __init__(
        self,
        *,
        messages_per_minute: int,
        callbacks_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._messages = messages_per_minute
        self._callbacks = callbacks_per_minute
        self._limiter = SlidingWindowLimiter(clock)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = data.get("event_from_user")
        if from_user is None or from_user.is_bot:
            return await handler(event, data)

        if isinstance(event, Message) and not self._limiter.allow(
            "message", from_user.id, limit=self._messages
        ):
            log.info("rate_limit_exceeded", user_id=from_user.id, kind="message")
            await event.answer(texts.too_many_requests())
            return None
        if isinstance(event, CallbackQuery) and not self._limiter.allow(
            "callback", from_user.id, limit=self._callbacks
        ):
            log.info("rate_limit_exceeded", user_id=from_user.id, kind="callback")
            await event.answer(texts.too_many_requests(), show_alert=True)
            return None

        return await handler(event, data)
