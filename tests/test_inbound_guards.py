"""Inbound guards: per-user rate limit and duplicate-message suppression."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from aiogram import Dispatcher
from aiogram.types import CallbackQuery, Message

from eng_remember.bot import texts
from eng_remember.bot.middlewares.dedup import DuplicateMessageMiddleware
from eng_remember.bot.middlewares.rate_limit import RateLimitMiddleware


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def message_event(user_id: int, text: str) -> tuple[Any, dict[str, Any]]:
    event = Mock(spec=Message)
    event.text = text
    event.answers = []

    async def answer(content: str, **kwargs: Any) -> None:
        event.answers.append((content, kwargs))

    event.answer = answer
    return event, {"event_from_user": SimpleNamespace(id=user_id, is_bot=False)}


def callback_event(user_id: int) -> tuple[Any, dict[str, Any]]:
    event = Mock(spec=CallbackQuery)
    event.answers = []

    async def answer(content: str | None = None, **kwargs: Any) -> None:
        event.answers.append((content, kwargs))

    event.answer = answer
    return event, {"event_from_user": SimpleNamespace(id=user_id, is_bot=False)}


def test_dispatcher_registers_the_guards_first(dispatcher: Dispatcher) -> None:
    message_guards = dispatcher.message.middleware._middlewares[:2]
    callback_guards = dispatcher.callback_query.middleware._middlewares[:2]

    assert isinstance(message_guards[0], RateLimitMiddleware)
    assert isinstance(message_guards[1], DuplicateMessageMiddleware)
    assert isinstance(callback_guards[0], RateLimitMiddleware)
    assert isinstance(callback_guards[1], DuplicateMessageMiddleware)


async def test_ten_messages_per_minute_are_allowed() -> None:
    clock = Clock()
    middleware = RateLimitMiddleware(
        messages_per_minute=10, callbacks_per_minute=30, clock=clock.now
    )
    handled: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> None:
        handled.append(event)

    first, first_data = message_event(1, "word")
    for _ in range(10):
        await middleware(handler, first, first_data)

    eleventh, eleventh_data = message_event(1, "word")
    await middleware(handler, eleventh, eleventh_data)

    assert len(handled) == 10
    assert eleventh.answers == [(texts.too_many_requests(), {})]


async def test_the_message_limit_resets_after_a_minute() -> None:
    clock = Clock()
    middleware = RateLimitMiddleware(
        messages_per_minute=10, callbacks_per_minute=30, clock=clock.now
    )
    handled: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> None:
        handled.append(event)

    for _ in range(10):
        await middleware(handler, *message_event(1, "word"))

    clock.advance(61)
    await middleware(handler, *message_event(1, "word"))

    assert len(handled) == 11


async def test_different_users_have_separate_message_limits() -> None:
    clock = Clock()
    middleware = RateLimitMiddleware(
        messages_per_minute=1, callbacks_per_minute=30, clock=clock.now
    )
    handled: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> None:
        handled.append(event)

    await middleware(handler, *message_event(1, "word"))
    await middleware(handler, *message_event(2, "word"))

    assert len(handled) == 2


async def test_callbacks_have_a_separate_limit_and_alert() -> None:
    clock = Clock()
    middleware = RateLimitMiddleware(
        messages_per_minute=10, callbacks_per_minute=30, clock=clock.now
    )
    handled: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> None:
        handled.append(event)

    for _ in range(30):
        await middleware(handler, *callback_event(1))

    extra, extra_data = callback_event(1)
    await middleware(handler, extra, extra_data)

    assert len(handled) == 30
    assert extra.answers == [(texts.too_many_requests(), {"show_alert": True})]


async def test_same_text_within_the_ttl_is_rejected() -> None:
    clock = Clock()
    middleware = DuplicateMessageMiddleware(ttl_seconds=60, clock=clock.now)
    handled: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> None:
        handled.append(event)

    first, first_data = message_event(1, "resilient — стойкий")
    await middleware(handler, first, first_data)

    duplicate, duplicate_data = message_event(1, "  resilient —   стойкий ")
    await middleware(handler, duplicate, duplicate_data)

    assert handled == [first]
    assert duplicate.answers == [(texts.duplicate_message(), {})]


async def test_same_text_after_the_ttl_is_allowed() -> None:
    clock = Clock()
    middleware = DuplicateMessageMiddleware(ttl_seconds=60, clock=clock.now)
    handled: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> None:
        handled.append(event)

    await middleware(handler, *message_event(1, "run — бежать"))
    clock.advance(61)
    await middleware(handler, *message_event(1, "run — бежать"))

    assert len(handled) == 2


async def test_commands_are_not_deduplicated() -> None:
    clock = Clock()
    middleware = DuplicateMessageMiddleware(ttl_seconds=60, clock=clock.now)
    handled: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> None:
        handled.append(event)

    await middleware(handler, *message_event(1, "/review"))
    await middleware(handler, *message_event(1, "/review"))

    assert len(handled) == 2


async def test_duplicate_guard_is_per_user() -> None:
    clock = Clock()
    middleware = DuplicateMessageMiddleware(ttl_seconds=60, clock=clock.now)
    handled: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> None:
        handled.append(event)

    await middleware(handler, *message_event(1, "run — бежать"))
    await middleware(handler, *message_event(2, "run — бежать"))

    assert len(handled) == 2


async def test_a_failed_handler_does_not_poison_the_duplicate_guard() -> None:
    clock = Clock()
    middleware = DuplicateMessageMiddleware(ttl_seconds=60, clock=clock.now)
    handled: list[Any] = []
    attempts = 0

    async def handler(event: Any, data: dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("handler failed")
        handled.append(event)

    with pytest.raises(RuntimeError):
        await middleware(handler, *message_event(1, "run — бежать"))
    await middleware(handler, *message_event(1, "run — бежать"))

    assert len(handled) == 1


async def test_zero_ttl_disables_the_duplicate_guard() -> None:
    clock = Clock()
    middleware = DuplicateMessageMiddleware(ttl_seconds=0, clock=clock.now)
    handled: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> None:
        handled.append(event)

    await middleware(handler, *message_event(1, "run — бежать"))
    await middleware(handler, *message_event(1, "run — бежать"))

    assert len(handled) == 2
