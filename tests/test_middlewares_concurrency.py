"""Per-learner concurrency limit: one account cannot fill every dispatcher slot.

The middleware is driven directly here: a real handler fast enough to run inside
a test cannot show who waits and who does not. Handlers park on an
``asyncio.Event`` instead, so "the fourth update waits" is observed, not timed
by luck.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from aiogram.types import Message, TelegramObject
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot.dispatcher import build_dispatcher
from eng_remember.bot.middlewares.concurrency import (
    UserConcurrencyMiddleware,
    UserSemaphoreRegistry,
)
from eng_remember.bot.middlewares.db import DbSessionMiddleware
from eng_remember.bot.middlewares.rate_limit import RateLimitMiddleware
from eng_remember.config import BotSettings, Settings

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def inbound(user_id: int) -> tuple[TelegramObject, dict[str, Any]]:
    """An event and the middleware data aiogram would hand over."""
    event = Mock(spec=Message)
    data: dict[str, Any] = {
        "event_from_user": SimpleNamespace(id=user_id, is_bot=False),
    }
    return event, data


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    """Yield until ``predicate`` holds, failing rather than hanging the suite."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition was not reached in time"
        await asyncio.sleep(0.001)


class Recorder:
    """Counts entries per learner and holds every handler until released."""

    def __init__(self) -> None:
        self.entries: list[int] = []
        self.release = asyncio.Event()

    def handler(self, user_id: int) -> Handler:
        async def handle(event: TelegramObject, data: dict[str, Any]) -> None:
            del event, data
            self.entries.append(user_id)
            await self.release.wait()

        return handle

    def started(self) -> int:
        return len(self.entries)


async def test_fourth_concurrent_update_waits() -> None:
    middleware = UserConcurrencyMiddleware(limit=3)
    recorder = Recorder()
    handler = recorder.handler(1)

    async def run() -> None:
        event, data = inbound(1)
        await middleware(handler, event, data)

    tasks = [asyncio.create_task(run()) for _ in range(3)]
    await wait_until(lambda: recorder.started() == 3)

    fourth = asyncio.create_task(run())
    await asyncio.sleep(0.01)

    assert recorder.started() == 3
    assert not fourth.done()

    recorder.release.set()
    await asyncio.gather(*tasks, fourth)

    assert recorder.started() == 4
    assert middleware.size == 0


async def test_different_users_not_blocked() -> None:
    # The strictest limit: any sharing between accounts would show up here.
    middleware = UserConcurrencyMiddleware(limit=1)
    recorder = Recorder()

    async def run(user_id: int) -> None:
        event, data = inbound(user_id)
        await middleware(recorder.handler(user_id), event, data)

    first = asyncio.create_task(run(1))
    second = asyncio.create_task(run(2))

    await wait_until(lambda: sorted(recorder.entries) == [1, 2])
    assert not first.done()
    assert not second.done()

    recorder.release.set()
    await asyncio.gather(first, second)

    assert middleware.size == 0


async def test_cancelled_waiter_does_not_break_the_registry() -> None:
    middleware = UserConcurrencyMiddleware(limit=1)
    recorder = Recorder()

    async def run() -> None:
        event, data = inbound(1)
        await middleware(recorder.handler(1), event, data)

    holder = asyncio.create_task(run())
    await wait_until(lambda: recorder.started() == 1)
    waiter = asyncio.create_task(run())
    await asyncio.sleep(0.01)
    assert not waiter.done()

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    # The cancelled waiter must not keep the learner's semaphore alive.
    assert middleware.size == 1

    recorder.release.set()
    await holder
    assert middleware.size == 0

    # And the account is still usable: a fresh update is not stuck behind the
    # semaphore the cancelled waiter was queued on.
    event, data = inbound(1)

    async def fresh_handler(event: TelegramObject, data: dict[str, Any]) -> str:
        del event, data
        return "ok"

    assert await asyncio.wait_for(middleware(fresh_handler, event, data), timeout=1) == "ok"
    assert middleware.size == 0


async def test_semaphore_is_released_when_the_handler_raises() -> None:
    middleware = UserConcurrencyMiddleware(limit=1)

    async def failing(event: TelegramObject, data: dict[str, Any]) -> None:
        del event, data
        raise RuntimeError("boom")

    event, data = inbound(1)
    with pytest.raises(RuntimeError):
        await middleware(failing, event, data)

    assert middleware.size == 0


async def test_registry_forgets_the_semaphore_after_the_last_holder() -> None:
    registry = UserSemaphoreRegistry(limit=2)

    async with registry.hold(1):
        assert registry.size == 1

    assert registry.size == 0

    # A later update for the same account creates a fresh, unheld semaphore.
    async with registry.hold(1):
        assert registry.size == 1
    assert registry.size == 0


def test_user_concurrency_limit_defaults_to_three() -> None:
    assert BotSettings().user_concurrency_limit == 3


def test_user_concurrency_limit_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        BotSettings(user_concurrency_limit=0)


def test_dispatcher_limits_after_the_rate_limit_and_before_the_session(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dispatcher = build_dispatcher(settings, session_factory)

    for observer in (dispatcher.message, dispatcher.callback_query):
        kinds = [type(item) for item in observer.middleware]
        assert UserConcurrencyMiddleware in kinds
        concurrency_at = kinds.index(UserConcurrencyMiddleware)
        # A flood is refused before it takes a per-learner slot, and a waiting
        # update holds no database session.
        assert kinds.index(RateLimitMiddleware) < concurrency_at
        assert kinds.index(DbSessionMiddleware) > concurrency_at
