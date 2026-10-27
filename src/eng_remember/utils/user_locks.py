"""Process-local locks that keep one learner's work sequential.

A Telegram update may arrive while the previous update from the same chat is
still waiting on a model answer. Processing both at once makes the quota check
read stale usage and lets a single account fill every dispatcher slot with
parallel model calls. The quota gate holds this lock around each call, so one
learner has at most one request in flight; non-model actions stay responsive.

Generated locks are removed as soon as the last holder leaves, so a long-running
process does not accumulate one object per Telegram account.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

__all__ = ["UserLockRegistry", "user_locks"]


class UserLockRegistry:
    """One ``asyncio.Lock`` per learner, created and discarded on demand."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._holders: dict[int, int] = {}

    @asynccontextmanager
    async def hold(self, user_id: int) -> AsyncIterator[None]:
        """Serialize everything running under ``user_id`` until the block exits."""
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        self._holders[user_id] = self._holders.get(user_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._holders[user_id] - 1
            if remaining:
                self._holders[user_id] = remaining
            else:
                self._holders.pop(user_id, None)
                self._locks.pop(user_id, None)

    @property
    def size(self) -> int:
        """How many learners currently have a live lock (zero at rest)."""
        return len(self._locks)


user_locks = UserLockRegistry()
