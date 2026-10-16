"""The reminder side of the bot: the command, and delivery through Telegram."""

from __future__ import annotations

from datetime import date

import pytest
from aiogram import Bot, Dispatcher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot import texts
from eng_remember.bot.notifier import BotNotifier
from eng_remember.domain.reminders import ReminderKind
from eng_remember.errors import RecipientUnreachable
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from eng_remember.services.reminders import ReminderPlan, WeeklyDigest
from eng_remember.utils.time import parse_clock
from tests.conftest import RecordingSession
from tests.factories import TEST_USER_ID
from tests.test_bot_flow import send


async def with_card(services: Services, user: User) -> None:
    await services.card_service.add_draft(
        user, CardDraft(term="ubiquitous", translation="вездесущий")
    )
    await services.commit()


def plan_for(user: User) -> ReminderPlan:
    return ReminderPlan(
        user=user,
        kind=ReminderKind.MORNING,
        local_day=date(2026, 5, 14),
        due_now=12,
        new_available=3,
        streak_days=7,
    )


class TestClockParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("8:30", (8, 30)), ("08:30", (8, 30)), ("21:05", (21, 5)), ("00:00", (0, 0))],
    )
    def test_valid_times(self, raw: str, expected: tuple[int, int]) -> None:
        parsed = parse_clock(raw)

        assert parsed is not None
        assert (parsed.hour, parsed.minute) == expected

    @pytest.mark.parametrize("raw", ["8", "вечером", "25:00", "9:60", "", "8:", ":30"])
    def test_invalid_times_are_rejected(self, raw: str) -> None:
        """Guessing 08:00 for someone who meant 20:00 is worse than asking again."""
        assert parse_clock(raw) is None


class TestRemindCommand:
    async def test_it_shows_the_current_settings(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, "/remind")

        sent = recording.last_text()
        assert "08:00" in sent
        assert "18:00" in sent
        assert "включены" in sent

    async def test_the_morning_time_can_be_changed(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/remind 7:15")

        async with session_factory() as check:
            user = (await check.scalars(select(User))).one()
            assert user.reminder_time is not None
            assert (user.reminder_time.hour, user.reminder_time.minute) == (7, 15)
        assert "07:15" in recording.last_text()

    async def test_the_evening_warning_can_be_moved(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, "/remind evening 22:45")

        assert "22:45" in recording.last_text()

    async def test_the_evening_warning_can_be_switched_off(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/remind evening off")

        async with session_factory() as check:
            user = (await check.scalars(select(User))).one()
            assert not user.evening_reminder
        assert "Вечернее: выключено" in recording.last_text()

    async def test_sending_them_can_be_turned_off(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/remind off")

        async with session_factory() as check:
            user = (await check.scalars(select(User))).one()
            assert not user.reminders_enabled
        assert "выключены" in recording.last_text()

    async def test_the_digest_can_be_switched_off(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/remind digest off")

        async with session_factory() as check:
            user = (await check.scalars(select(User))).one()
            assert not user.digest_enabled
        assert "Дайджест: выключен" in recording.last_text()

    async def test_the_digest_can_be_switched_back_on(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/remind digest off", update_id=10)
        await send(dispatcher, bot, "/remind digest on", update_id=11)

        async with session_factory() as check:
            user = (await check.scalars(select(User))).one()
            assert user.digest_enabled
        assert "Дайджест: включён" in recording.last_text()

    async def test_garbage_gets_the_usage_rather_than_a_guess(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, "/remind когда-нибудь утром")

        assert "/remind 8:30" in recording.last_text()

    async def test_settings_summary_mentions_reminders(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, "/settings")

        assert "Напоминания" in recording.last_text()


class TestNotifier:
    async def test_a_reminder_reaches_the_chat(
        self,
        bot: Bot,
        recording: RecordingSession,
        user: User,
        services: Services,
    ) -> None:
        await with_card(services, user)
        notifier = BotNotifier(bot)

        await notifier.send_reminder(plan_for(user))

        assert recording.last_text() == texts.queue_reminder(
            due_now=12, new_available=3, streak_days=7
        )

    async def test_a_streak_warning_reads_differently(
        self, bot: Bot, recording: RecordingSession, user: User
    ) -> None:
        plan = ReminderPlan(
            user=user,
            kind=ReminderKind.EVENING,
            local_day=date(2026, 5, 14),
            due_now=0,
            new_available=5,
            streak_days=9,
            streak_warning=True,
        )

        await BotNotifier(bot).send_reminder(plan)

        sent = recording.last_text()
        assert "под угрозой" in sent
        assert "9" in sent

    async def test_an_evening_without_risk_is_a_plain_check(
        self, bot: Bot, recording: RecordingSession, user: User
    ) -> None:
        plan = ReminderPlan(
            user=user,
            kind=ReminderKind.EVENING,
            local_day=date(2026, 5, 14),
            due_now=4,
            new_available=0,
            streak_days=9,
            streak_warning=False,
        )

        await BotNotifier(bot).send_reminder(plan)

        sent = recording.last_text()
        assert "Вечерняя проверка" in sent
        assert "под угрозой" not in sent
        assert "4" in sent

    async def test_a_digest_reads_as_a_week_summary(
        self, bot: Bot, recording: RecordingSession, user: User
    ) -> None:
        digest = WeeklyDigest(
            days=(0,) * 27 + (3,),
            total=12,
            correct=9,
            new_words=5,
            weak=(("ubiquitous", 4),),
            due_now=2,
            due_week=7,
        )
        plan = ReminderPlan(
            user=user,
            kind=ReminderKind.DIGEST,
            local_day=date(2026, 5, 14),
            due_now=2,
            new_available=0,
            streak_days=6,
            digest=digest,
        )

        await BotNotifier(bot).send_reminder(plan)

        sent = recording.last_text()
        assert "Итоги недели" in sent
        assert "12" in sent
        assert "ubiquitous" in sent
        assert "🟨" in sent

    async def test_a_blocked_bot_is_reported_as_unreachable(
        self, bot: Bot, recording: RecordingSession, user: User
    ) -> None:
        """The distinction the service relies on: final, not transient."""
        recording.forbidden.add(TEST_USER_ID)

        with pytest.raises(RecipientUnreachable):
            await BotNotifier(bot).send_reminder(plan_for(user))


class TestMessageTexts:
    def test_the_morning_message_lists_what_is_waiting(self) -> None:
        sent = texts.queue_reminder(due_now=12, new_available=3, streak_days=7)

        assert "12" in sent
        assert "3" in sent
        assert "7" in sent

    def test_it_mentions_only_what_exists(self) -> None:
        only_new = texts.queue_reminder(due_now=0, new_available=4, streak_days=0)

        assert "новых — <b>4</b>" in only_new
        assert "Серия" not in only_new

    def test_the_streak_warning_names_the_stake(self) -> None:
        sent = texts.evening_reminder(
            streak_days=14, due_now=5, new_available=0, streak_warning=True
        )

        assert "14" in sent
        assert "под угрозой" in sent
        assert "5" in sent

    def test_it_stays_calm_when_there_is_nothing_queued(self) -> None:
        sent = texts.evening_reminder(
            streak_days=3, due_now=0, new_available=0, streak_warning=True
        )

        assert "под угрозой" in sent
        assert "Ждут" not in sent
