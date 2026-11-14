"""FSM-aware guards: an active flow is neither a duplicate nor a flood.

These tests build the middlewares themselves from production defaults instead of
using the ``settings`` fixture, which deliberately switches the guards off for
handler scenarios. The point here is exactly the guard-on behaviour.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, TelegramObject
from pydantic import SecretStr

from eng_remember.bot import texts
from eng_remember.bot.middlewares.dedup import DuplicateMessageMiddleware
from eng_remember.bot.middlewares.rate_limit import RateLimitMiddleware
from eng_remember.bot.states import Review
from eng_remember.config import Settings
from tests.factories import make_message_update


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeState:
    """The bits of ``FSMContext`` the guards touch: just the current state."""

    def __init__(self, value: str | None) -> None:
        self.value = value

    async def get_state(self) -> str | None:
        return self.value


def real_settings() -> Settings:
    """Production defaults — guards are switched on."""
    return Settings(_env_file=None, bot_token=SecretStr("42:TEST-TOKEN"), log_json=False)


def message_event(
    user_id: int, text: str, *, state: FakeState | None = None
) -> tuple[Any, dict[str, Any]]:
    event = Mock(spec=Message)
    event.text = text
    event.answers = []

    async def answer(content: str, **kwargs: Any) -> None:
        event.answers.append((content, kwargs))

    event.answer = answer
    data: dict[str, Any] = {"event_from_user": SimpleNamespace(id=user_id, is_bot=False)}
    if state is not None:
        data["state"] = state
        data["raw_state"] = state.value
    return event, data


class Recorder:
    """A handler that just remembers what reached it."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, event: Any, data: dict[str, Any]) -> None:
        self.events.append(event)


def dedup(settings: Settings, clock: Clock) -> DuplicateMessageMiddleware:
    return DuplicateMessageMiddleware(
        ttl_seconds=settings.bot.duplicate_message_ttl_seconds, clock=clock.now
    )


def limiter(settings: Settings, clock: Clock) -> RateLimitMiddleware:
    return RateLimitMiddleware(
        messages_per_minute=settings.bot.messages_per_minute,
        callbacks_per_minute=settings.bot.callbacks_per_minute,
        clock=clock.now,
    )


async def test_identical_review_answers_not_deduped() -> None:
    """Review Focus 2: a repeated translation is a legitimate answer."""
    settings = real_settings()
    clock = Clock()
    middleware = dedup(settings, clock)
    handler = Recorder()
    state = FakeState(Review.awaiting_answer.state)

    await middleware(handler, *message_event(1, "resilient", state=state))
    second, second_data = message_event(1, "resilient", state=state)
    await middleware(handler, second, second_data)

    assert len(handler.events) == 2
    assert second.answers == []


@pytest.mark.parametrize("button", texts.MAIN_MENU_BUTTONS)
async def test_menu_button_press_not_deduped(button: str) -> None:
    """Tapping the same menu button again within the window must work."""
    settings = real_settings()
    clock = Clock()
    middleware = dedup(settings, clock)
    handler = Recorder()

    await middleware(handler, *message_event(1, button))
    clock.advance(30)

    second, second_data = message_event(1, button)
    await middleware(handler, second, second_data)

    assert len(handler.events) == 2
    assert second.answers == []


async def test_retry_after_parse_error_not_deduped() -> None:
    """A handled refusal is not a saved card: the same text can be retried at once."""
    settings = real_settings()
    clock = Clock()
    middleware = dedup(settings, clock)
    handler = Recorder()
    state = FakeState(None)

    await middleware(handler, *message_event(1, "asdfgh", state=state))
    second, second_data = message_event(1, "asdfgh", state=state)
    await middleware(handler, second, second_data)

    assert len(handler.events) == 2
    assert second.answers == []


async def test_a_message_that_starts_a_flow_keeps_its_dedup_key() -> None:
    """Once a message becomes a pending FSM flow, the guard keeps counting it."""
    settings = real_settings()
    clock = Clock()
    middleware = dedup(settings, clock)
    state = FakeState(None)
    handled: list[Any] = []

    async def start_flow(event: Any, data: dict[str, Any]) -> None:
        handled.append(event)
        state.value = Review.awaiting_answer.state

    await middleware(start_flow, *message_event(1, "resilient", state=state))
    clock.advance(10)

    # The state is active for the learner, but the middleware is asked again on
    # a fresh event whose raw_state was read before the handler changed it.
    duplicate, duplicate_data = message_event(1, "resilient", state=FakeState(None))
    await middleware(start_flow, duplicate, duplicate_data)

    assert len(handled) == 1
    assert duplicate.answers == [(texts.duplicate_message(), {})]


async def test_dedup_prunes_expired_keys_on_every_call() -> None:
    """Stale keys go on the next message, not only after a thousand of them."""
    settings = real_settings()
    clock = Clock()
    middleware = dedup(settings, clock)

    async def stay_in_flow(event: Any, data: dict[str, Any]) -> None:
        data["state"].value = Review.awaiting_answer.state

    for index in range(5):
        await middleware(stay_in_flow, *message_event(1, f"word {index}", state=FakeState(None)))

    assert len(middleware._seen) == 5

    clock.advance(settings.bot.duplicate_message_ttl_seconds + 1)
    await middleware(stay_in_flow, *message_event(1, "fresh", state=FakeState(None)))

    assert list(middleware._seen) == [(1, "fresh")]


async def test_active_fsm_exempt_from_rate_limit() -> None:
    """A review or practice run can send more than a minute's worth of answers."""
    settings = real_settings()
    clock = Clock()
    middleware = limiter(settings, clock)
    handler = Recorder()
    state = FakeState(Review.awaiting_answer.state)
    events: list[Any] = []

    for _ in range(settings.bot.messages_per_minute + 5):
        event, event_data = message_event(1, "answer", state=state)
        events.append(event)
        await middleware(handler, event, event_data)

    assert len(handler.events) == settings.bot.messages_per_minute + 5
    assert [event.answers for event in events] == [[] for _ in events]


async def test_rate_limit_reply_throttled() -> None:
    """A flood gets one refusal per window, not one per dropped message."""
    settings = real_settings()
    clock = Clock()
    middleware = limiter(settings, clock)
    handler = Recorder()

    first_window: list[Any] = []
    for _ in range(settings.bot.messages_per_minute + 20):
        event, event_data = message_event(1, "word")
        await middleware(handler, event, event_data)
        first_window.extend(event.answers)

    assert len(handler.events) == settings.bot.messages_per_minute
    assert first_window == [(texts.too_many_requests(), {})]

    clock.advance(61)
    second_window: list[Any] = []
    for _ in range(settings.bot.messages_per_minute + 5):
        event, event_data = message_event(1, "word")
        await middleware(handler, event, event_data)
        second_window.extend(event.answers)

    assert second_window == [(texts.too_many_requests(), {})]


async def test_rate_limit_prunes_stale_buckets() -> None:
    """Learners who stop writing do not leave their buckets behind forever."""
    settings = real_settings()
    clock = Clock()
    middleware = limiter(settings, clock)
    handler = Recorder()

    for user_id in range(99):
        await middleware(handler, *message_event(user_id, "word"))

    clock.advance(61)
    # The hundredth call triggers the sweep, which drops the stale 99 first.
    await middleware(handler, *message_event(99, "word"))

    buckets = middleware._limiter._buckets
    assert set(buckets) == {("message", 99)}


async def test_fsm_state_is_visible_to_event_middlewares(bot: Bot) -> None:
    """The guards run below the FSM middleware, so ``raw_state`` is already there.

    Guards are registered on the ``message`` observer, while aiogram wires
    ``FSMContextMiddleware`` on the ``update`` observer; this test pins that
    ordering, because both guards depend on it.
    """
    dispatcher = Dispatcher(storage=MemoryStorage())
    seen: list[str | None] = []

    class Probe(BaseMiddleware):
        async def __call__(
            self,
            handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
            event: TelegramObject,
            data: dict[str, Any],
        ) -> Any:
            seen.append(data.get("raw_state"))
            return await handler(event, data)

    router = Router()

    @router.message(Command("begin"))
    async def begin(message: Message, state: FSMContext) -> None:
        await state.set_state(Review.awaiting_answer)

    @router.message()
    async def echo(message: Message) -> None:
        await message.answer("ok")

    dispatcher.message.middleware(Probe())
    dispatcher.include_router(router)

    await dispatcher.feed_update(bot, make_message_update("/begin", update_id=1))
    await dispatcher.feed_update(bot, make_message_update("resilient", update_id=2))

    assert seen == [None, Review.awaiting_answer.state]
