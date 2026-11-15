"""The cron entry point does its own housekeeping.

The README tells multi-replica deployments to switch the in-process loop off and
drive reminders from cron. With no loop there is no daily prune either, so the
one-shot entry point has to run its own: the audit table must not grow without
bound just because of the deployment shape. And because the switch is about the
loop, not about reminders as a whole, the cron pass must still send when it is
off.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time

import pytest
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import eng_remember.__main__ as entrypoint
import eng_remember.remind as remind_entry
from eng_remember.__main__ import prune_llm_calls_if_due
from eng_remember.config import Settings
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from tests.conftest import RecordingSession
from tests.factories import TEST_USER_ID, schedule


class _DisposableEngine:
    """Stands in for the real engine: the entry point must dispose it."""

    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


async def test_cron_prunes_even_with_the_in_process_loop_off(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
) -> None:
    """``REMINDERS__ENABLED=false`` is exactly the deployment that needs it.

    The documented multi-replica setup has no loop, so the cron pass is the only
    place retention can happen; being an explicit "send what is due" command
    must not silently skip the housekeeping.
    """
    settings.reminders.enabled = False
    pruned: list[tuple[object, ...]] = []

    async def spy(*args: object, **kwargs: object) -> None:
        pruned.append(args)

    engine = _DisposableEngine()
    monkeypatch.setattr(remind_entry, "get_settings", lambda: settings)
    monkeypatch.setattr(remind_entry, "create_engine", lambda _: engine)
    monkeypatch.setattr(remind_entry, "create_session_factory", lambda _: session_factory)
    monkeypatch.setattr(remind_entry, "Bot", lambda **_: bot)
    monkeypatch.setattr(remind_entry, "prune_llm_calls_if_due", spy)

    assert await remind_entry.main() == 0
    assert len(pruned) == 1
    assert engine.disposed


async def test_the_prune_runs_at_most_once_per_utc_day(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two ticks on the same UTC date share one prune; midnight starts a new one.

    The boundary is the UTC date, not a monotonic timer: it survives a restart
    and matches the timezone the audit column stores.
    """
    settings.llm_call_retention_days = 90
    calls: list[datetime | None] = []

    async def fake(*args: object, now: datetime | None = None, **kwargs: object) -> int:
        calls.append(now)
        return 0

    monkeypatch.setattr(entrypoint, "prune_llm_calls", fake)

    morning = datetime(2026, 11, 15, 3, 0, tzinfo=UTC)
    evening = datetime(2026, 11, 15, 23, 0, tzinfo=UTC)
    tomorrow = datetime(2026, 11, 16, 0, 1, tzinfo=UTC)

    stamp = await prune_llm_calls_if_due(session_factory, settings, None, now=morning)
    assert stamp == date(2026, 11, 15)
    assert await prune_llm_calls_if_due(session_factory, settings, stamp, now=evening) == stamp
    assert len(calls) == 1

    assert await prune_llm_calls_if_due(session_factory, settings, stamp, now=tomorrow) == date(
        2026, 11, 16
    )
    assert len(calls) == 2


async def test_a_failed_prune_is_retried_instead_of_skipping_the_day(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A database hiccup must not cost the day's cleanup, and must not bubble."""
    settings.llm_call_retention_days = 90
    attempts: list[int] = []

    async def broken(*args: object, **kwargs: object) -> int:
        attempts.append(1)
        raise RuntimeError("database went away")

    monkeypatch.setattr(entrypoint, "prune_llm_calls", broken)

    now = datetime(2026, 11, 15, 3, 0, tzinfo=UTC)
    stamp = await prune_llm_calls_if_due(session_factory, settings, None, now=now)
    assert stamp is None

    later = await prune_llm_calls_if_due(session_factory, settings, stamp, now=now)
    assert later is None
    assert len(attempts) == 2


async def test_zero_days_never_prunes(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings.llm_call_retention_days = 0
    calls: list[int] = []

    async def fake(*args: object, **kwargs: object) -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr(entrypoint, "prune_llm_calls", fake)

    stamp = await prune_llm_calls_if_due(session_factory, settings, None, now=datetime.now(UTC))

    assert stamp is None
    assert calls == []


async def test_cron_sends_even_with_the_in_process_loop_off(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
) -> None:
    """``REMINDERS__ENABLED=false`` switches the loop off, not the command.

    The documented multi-replica deployment relies on cron being the sender;
    treating the flag as a global kill switch left that deployment silently
    sending nothing at all.
    """
    settings.reminders.enabled = False
    user.reminder_time = time(0, 0)
    deck = await services.card_service.default_deck(user)
    await services.cards.add(
        user_id=user.id,
        deck_id=deck.id,
        draft=CardDraft(term="cron", translation="крон"),
        schedule=schedule(-1),
    )
    await session.commit()

    engine = _DisposableEngine()
    monkeypatch.setattr(remind_entry, "get_settings", lambda: settings)
    monkeypatch.setattr(remind_entry, "create_engine", lambda _: engine)
    monkeypatch.setattr(remind_entry, "create_session_factory", lambda _: session_factory)
    monkeypatch.setattr(remind_entry, "Bot", lambda **_: bot)

    assert await remind_entry.main() == 0
    assert len(recording.outbound) == 1
    assert recording.outbound[0].chat_id == TEST_USER_ID
    assert engine.disposed


class _PollingDispatcher:
    """Stands in for the dispatcher: polling returns at once, as if told to stop."""

    def __init__(self) -> None:
        self.polled = False

    def resolve_used_update_types(self) -> list[str]:
        return []

    async def start_polling(self, *args: object, **kwargs: object) -> None:
        # Yield once so tasks created just before polling get to run.
        await asyncio.sleep(0.01)
        self.polled = True


@pytest.mark.parametrize("enabled", [False, True])
async def test_the_in_process_loop_starts_only_when_reminders_are_enabled(
    enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
) -> None:
    """The flag belongs to the process: cron remains an explicit command.

    The enabled case is the control: without it a broken harness could pass the
    disabled one too.
    """
    settings = settings.model_copy(
        update={"reminders": settings.reminders.model_copy(update={"enabled": enabled})}
    )
    engine = _DisposableEngine()
    dispatcher = _PollingDispatcher()
    started: list[str] = []

    async def fake_loop(**kwargs: object) -> None:
        started.append("reminder-loop")

    monkeypatch.setattr(entrypoint, "get_settings", lambda: settings)
    monkeypatch.setattr(entrypoint, "create_engine", lambda _: engine)
    monkeypatch.setattr(entrypoint, "create_session_factory", lambda _: session_factory)
    monkeypatch.setattr(entrypoint, "build_dispatcher", lambda *a, **k: dispatcher)
    monkeypatch.setattr(entrypoint, "Bot", lambda **_: bot)
    monkeypatch.setattr(entrypoint, "reminder_loop", fake_loop)

    await entrypoint.main()

    assert dispatcher.polled
    assert engine.disposed
    assert bool(started) == enabled
