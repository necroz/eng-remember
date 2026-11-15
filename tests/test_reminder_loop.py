"""The in-process reminder loop: one crash must not cost the whole tick or the loop.

The service itself is covered in ``test_services_reminders.py``; this module
drives the entry point that production runs, with a recording notifier and a
session factory that knows when to stop the loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import eng_remember.__main__ as entrypoint
from eng_remember.__main__ import reminder_loop
from eng_remember.config import Settings
from eng_remember.db.engine import SessionFactory
from eng_remember.models.user import User
from eng_remember.repositories.users import TelegramProfile, UserRepository
from eng_remember.services.container import Services
from eng_remember.services.reminders import (
    ReminderPlan,
    ReminderService,
    build_reminder_service,
)
from eng_remember.utils.time import local_date
from tests.factories import schedule

TODAY = local_date("UTC")


class RecordingNotifier:
    """A notifier that remembers what it sent, and can refuse one chat."""

    def __init__(self) -> None:
        self.sent: list[ReminderPlan] = []
        self.sent_ids: list[int] = []
        self.alerts: list[str] = []
        self.broken: set[int] = set()

    async def send_reminder(self, plan: ReminderPlan) -> None:
        if plan.user.id in self.broken:
            raise ConnectionError("network is down")
        self.sent.append(plan)
        self.sent_ids.append(plan.user.id)

    async def send_alert(self, text: str) -> None:
        self.alerts.append(text)


async def make_learner(services: Services, telegram_id: int) -> User:
    """A learner with one due card, so every tick has something to send.

    The reminder hour is pulled to midnight: the loop uses the real clock, and a
    test may run before the default 08:00.
    """
    from datetime import time

    from eng_remember.repositories.cards import CardDraft

    learner, _ = await services.users.get_or_create(TelegramProfile(telegram_id=telegram_id))
    learner.reminder_time = time(0, 0)
    deck = await services.card_service.default_deck(learner)
    await services.cards.add(
        user_id=learner.id,
        deck_id=deck.id,
        draft=CardDraft(term=f"word{telegram_id}", translation="слово"),
        schedule=schedule(-1),
    )
    await services.commit()
    return learner


def stopping_factory(
    factory: async_sessionmaker[AsyncSession],
    stopping: asyncio.Event,
    *,
    stop_at: int,
) -> SessionFactory:
    """Wrap a session factory and set ``stopping`` as the stop_at-th tick begins.

    The event is checked at the top of the loop, so setting it here still lets
    that tick run and only then ends the loop.
    """
    calls = 0

    @asynccontextmanager
    async def make() -> AsyncIterator[AsyncSession]:
        nonlocal calls
        calls += 1
        if calls == stop_at:
            stopping.set()
        async with factory() as session:
            yield session

    return cast(SessionFactory, make)


async def wait_for_sends(notifier: RecordingNotifier, count: int) -> None:
    for _ in range(500):
        if len(notifier.sent) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"notifier saw {len(notifier.sent)} sends, expected {count}")


async def test_crash_mid_tick_keeps_sent_watermarks(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    services: Services,
    settings: Settings,
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first batch is committed before the second one is attempted.

    A crash while walking page two must not roll page one back: those learners
    were really messaged, and sending the same reminder again on the next tick
    would be a duplicate. The second learner is retried, as no watermark exists.
    """
    first = await make_learner(services, 500001)
    second = await make_learner(services, 500002)
    settings.reminders.batch_size = 1
    # The loop sleeps between ticks; a zero interval keeps the test in seconds.
    settings.reminders.tick_seconds = 0
    notifier = RecordingNotifier()
    stopping = asyncio.Event()

    original = UserRepository.list_reminder_candidates
    calls = 0

    async def flaky(self: UserRepository, **kwargs: Any) -> list[User]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("database went away")
        return await original(self, **kwargs)

    monkeypatch.setattr(UserRepository, "list_reminder_candidates", flaky)

    await reminder_loop(
        bot=bot,
        session_factory=stopping_factory(session_factory, stopping, stop_at=2),
        settings=settings,
        stopping=stopping,
        notifier=notifier,
    )

    assert notifier.sent_ids == [first.id, second.id]
    async with session_factory() as fresh:
        rows = (await fresh.scalars(select(User).order_by(User.id))).all()
        assert [row.last_reminder_on for row in rows] == [TODAY, TODAY]


async def test_reminder_loop_survives_tick_exception(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    services: Services,
    settings: Settings,
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing tick is logged and the loop comes back for the next one."""
    learner = await make_learner(services, 500011)
    settings.reminders.tick_seconds = 0
    notifier = RecordingNotifier()
    stopping = asyncio.Event()

    original = build_reminder_service
    built = 0

    def flaky(*args: Any, **kwargs: Any) -> ReminderService:
        nonlocal built
        built += 1
        if built == 1:
            raise RuntimeError("boom")
        return original(*args, **kwargs)

    monkeypatch.setattr(entrypoint, "build_reminder_service", flaky)

    await reminder_loop(
        bot=bot,
        session_factory=stopping_factory(session_factory, stopping, stop_at=2),
        settings=settings,
        stopping=stopping,
        notifier=notifier,
    )

    assert notifier.sent_ids == [learner.id]


async def test_cancellation_stops_the_loop_promptly(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    services: Services,
    settings: Settings,
    bot: Bot,
) -> None:
    """``task.cancel()`` must not be swallowed by the tick's error handling."""
    learner = await make_learner(services, 500021)
    notifier = RecordingNotifier()
    stopping = asyncio.Event()

    task = asyncio.create_task(
        reminder_loop(
            bot=bot,
            session_factory=session_factory,
            settings=settings,
            stopping=stopping,
            notifier=notifier,
        )
    )
    try:
        await wait_for_sends(notifier, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert task.cancelled()
    assert notifier.sent_ids == [learner.id]


def test_the_bot_gets_a_grace_period_longer_than_the_llm_timeout() -> None:
    """Docker's default ten seconds would kill a batch mid-model-call."""
    from pathlib import Path

    compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    bot_section = text.split("\n  bot:", 1)[1]
    assert "stop_grace_period: 130s" in bot_section
