"""Per-learner inbound rate limit.

The global dispatcher semaphore bounds how many updates are processed at once,
but one account can still fill it with a burst. This middleware rejects beyond
a rolling per-minute allowance before a database session is opened, so a flood
costs neither a connection nor a model call.

Two things are exempt from the message budget: an active FSM flow, because a
review or practice run legitimately sends more answers than a minute's worth of
chat, and the refusal reply itself, which is sent at most once per window per
learner instead of once per dropped message.
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

__all__ = ["RateLimitMiddleware", "SlidingWindowLimiter"]

log = structlog.get_logger(__name__)
WINDOW_SECONDS = 60.0
# Walking every bucket on every message would make a busy bot quadratic. A
# periodic sweep is enough: the map only ever holds learners from the last
# window plus those the sweep has not reached yet.
_PRUNE_EVERY = 100


class SlidingWindowLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[tuple[str, int], deque[float]] = {}
        self._calls = 0

    def allow(self, kind: str, user_id: int, *, limit: int) -> bool:
        now = self._clock()
        cutoff = now - WINDOW_SECONDS
        self._calls += 1
        if self._calls % _PRUNE_EVERY == 0:
            self._prune(cutoff)
        bucket = self._buckets.setdefault((kind, user_id), deque())
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def _prune(self, cutoff: float) -> None:
        """Forget learners whose last message has left the window."""
        for key, bucket in list(self._buckets.items()):
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                del self._buckets[key]


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
        self._clock = clock
        self._limiter = SlidingWindowLimiter(clock)
        # When the learner last saw the refusal, so a flood yields one reply.
        self._refusals: dict[tuple[str, int], float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = data.get("event_from_user")
        if from_user is None or from_user.is_bot:
            return await handler(event, data)

        if isinstance(event, Message) and data.get("raw_state"):
            # An answer typed inside review, practice or capture is part of a
            # flow the learner already paid for; it must not trip the flood gate.
            return await handler(event, data)

        if isinstance(event, Message) and not self._limiter.allow(
            "message", from_user.id, limit=self._messages
        ):
            log.info("rate_limit_exceeded", user_id=from_user.id, kind="message")
            if self._should_reply(("message", from_user.id)):
                await event.answer(texts.too_many_requests())
            return None
        if isinstance(event, CallbackQuery) and not self._limiter.allow(
            "callback", from_user.id, limit=self._callbacks
        ):
            log.info("rate_limit_exceeded", user_id=from_user.id, kind="callback")
            if self._should_reply(("callback", from_user.id)):
                await event.answer(texts.too_many_requests(), show_alert=True)
            return None

        return await handler(event, data)

    def _should_reply(self, key: tuple[str, int]) -> bool:
        """True once per rolling window, however many messages were dropped."""
        now = self._clock()
        self._prune_refusals(now)
        last = self._refusals.get(key)
        if last is not None and now - last < WINDOW_SECONDS:
            return False
        self._refusals[key] = now
        return True

    def _prune_refusals(self, now: float) -> None:
        if len(self._refusals) < _PRUNE_EVERY:
            return
        cutoff = now - WINDOW_SECONDS
        for key, sent_at in list(self._refusals.items()):
            if sent_at <= cutoff:
                del self._refusals[key]
