"""Bound how many updates one learner may have in flight at once.

``tasks_concurrency_limit`` caps the whole process. Without a second cap one
account can still fill every slot with a burst of updates that all queue behind
the per-learner model lock: the quota gate serializes the model calls, but the
updates waiting for it keep their dispatcher slot, and other learners are left
with none. Each account therefore gets a small counting semaphore of its own, so
a burst occupies at most ``user_concurrency_limit`` slots and the rest wait.

The semaphores live in a registry that counts holders — waiting ones included —
and drops a semaphore as soon as its last holder leaves. A long-running process
does not accumulate an object per Telegram account, and a cancelled waiter
cannot leave a stale semaphore behind.

The limiter is registered after the rate limit and the duplicate guard, before
the session middleware: a flood is refused without taking a slot, and an update
parked here is not holding a database connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

__all__ = ["UserConcurrencyMiddleware", "UserSemaphoreRegistry"]


class UserSemaphoreRegistry:
    """One counting semaphore per learner, created and discarded on demand."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        self._semaphores: dict[int, asyncio.Semaphore] = {}
        self._holders: dict[int, int] = {}

    @asynccontextmanager
    async def hold(self, user_id: int) -> AsyncIterator[None]:
        """Allow at most ``limit`` blocks under ``user_id`` at a time.

        The holder count is raised before the semaphore is awaited, so a waiter
        keeps the pair alive too: removing on "no one is running" would drop the
        semaphore a queued task is still waiting on.
        """
        semaphore = self._semaphores.get(user_id)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._limit)
            self._semaphores[user_id] = semaphore
        self._holders[user_id] = self._holders.get(user_id, 0) + 1
        try:
            async with semaphore:
                yield
        finally:
            remaining = self._holders[user_id] - 1
            if remaining:
                self._holders[user_id] = remaining
            else:
                self._holders.pop(user_id, None)
                self._semaphores.pop(user_id, None)

    @property
    def size(self) -> int:
        """How many learners currently have a live semaphore (zero at rest)."""
        return len(self._semaphores)


class UserConcurrencyMiddleware(BaseMiddleware):
    """Keep one learner's updates from filling the dispatcher by themselves."""

    def __init__(self, *, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._registry = UserSemaphoreRegistry(limit)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = data.get("event_from_user")
        if from_user is None or from_user.is_bot:
            return await handler(event, data)

        async with self._registry.hold(from_user.id):
            return await handler(event, data)

    @property
    def size(self) -> int:
        """How many learners currently have a live semaphore (zero at rest)."""
        return self._registry.size
