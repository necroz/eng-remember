"""Praise and streaks as the learner experiences them.

The praise template is read back from what the bot persisted, then its own words
are looked for in the message — so these tests check that the template the bank
chose is the template the user saw, not merely that some text was sent.
"""

from __future__ import annotations

import re
from datetime import timedelta

from aiogram import Bot, Dispatcher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot.callbacks import ReviewCB
from eng_remember.domain.enums import ReviewRating
from eng_remember.domain.praise import BANKS, PraiseCategory
from eng_remember.models.card import Card
from eng_remember.models.streak import Streak
from eng_remember.models.user import User
from tests.conftest import RecordingSession
from tests.test_bot_flow import send, tap


def literal_chunks(template: str) -> list[str]:
    """The non-placeholder runs of a template, long enough to be a fingerprint."""
    return [chunk.strip() for chunk in re.split(r"\{\w+\}", template) if len(chunk.strip()) >= 5]


def answer_message(recording: RecordingSession) -> str:
    """The text of the graded card, not the summary that follows it."""
    for text in reversed(recording.sent_texts()):
        if "Дальше через" in text:
            return text
    raise AssertionError(f"no graded-card message among {recording.sent_texts()}")


async def grade(
    dispatcher: Dispatcher,
    bot: Bot,
    card_id: int,
    rating: ReviewRating,
    *,
    update_id: int = 200,
) -> None:
    await send(dispatcher, bot, "/review", update_id=update_id)
    await tap(
        dispatcher,
        bot,
        ReviewCB(action="show", card_id=card_id).pack(),
        update_id=update_id + 1,
    )
    await tap(
        dispatcher,
        bot,
        ReviewCB(action="grade", card_id=card_id, rating=int(rating)).pack(),
        update_id=update_id + 2,
    )


async def fresh_user(session_factory: async_sessionmaker[AsyncSession]) -> User:
    async with session_factory() as session:
        return (await session.scalars(select(User))).one()


class TestPraise:
    async def test_a_correct_answer_is_praised(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        saved_card: Card,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        card_id = saved_card.id

        await grade(dispatcher, bot, card_id, ReviewRating.GOOD)

        user = await fresh_user(session_factory)
        used = user.praise_history["good"]
        assert len(used) == 1

        template = BANKS[PraiseCategory.GOOD][used[0]]
        text = answer_message(recording)
        assert any(chunk in text for chunk in literal_chunks(template)), (
            f"template {template!r} not found in {text!r}"
        )

    async def test_easy_answers_use_their_own_bank(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        saved_card: Card,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await grade(dispatcher, bot, saved_card.id, ReviewRating.EASY)

        user = await fresh_user(session_factory)
        assert set(user.praise_history) == {"easy"}
        template = BANKS[PraiseCategory.EASY][user.praise_history["easy"][0]]
        assert any(chunk in answer_message(recording) for chunk in literal_chunks(template))

    async def test_a_missed_card_is_supported_not_praised(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        saved_card: Card,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await grade(dispatcher, bot, saved_card.id, ReviewRating.AGAIN)

        user = await fresh_user(session_factory)
        assert "again" in user.praise_history
        assert "good" not in user.praise_history
        template = BANKS[PraiseCategory.AGAIN][user.praise_history["again"][0]]
        assert any(chunk in answer_message(recording) for chunk in literal_chunks(template))

    async def test_two_answers_do_not_get_the_same_praise(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        saved_card: Card,
        session_factory: async_sessionmaker[AsyncSession],
        user: User,
    ) -> None:
        """Same card, answered twice: the template must change."""
        card_id = saved_card.id
        await grade(dispatcher, bot, card_id, ReviewRating.GOOD, update_id=300)
        await grade(dispatcher, bot, card_id, ReviewRating.GOOD, update_id=310)

        owner = await fresh_user(session_factory)
        used = owner.praise_history["good"]
        assert len(used) == 2
        assert used[0] != used[1]

    async def test_praise_history_persists_across_answers(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        saved_card: Card,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """It is a database column, not memory: a restart must not reset it."""
        card_id = saved_card.id
        for round_number in range(3):
            await grade(
                dispatcher, bot, card_id, ReviewRating.GOOD, update_id=400 + round_number * 10
            )

        owner = await fresh_user(session_factory)
        assert len(owner.praise_history["good"]) == 3
        assert len(set(owner.praise_history["good"])) == 3


class TestStreakInTheBot:
    async def test_the_first_answer_starts_a_streak(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        saved_card: Card,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await grade(dispatcher, bot, saved_card.id, ReviewRating.GOOD)

        async with session_factory() as session:
            streak = (await session.scalars(select(Streak))).one()
        assert streak.current == 1
        assert streak.accounted_on is not None
        assert "🔥 <b>1</b>" in answer_message(recording)

    async def test_a_second_answer_the_same_day_does_not_extend_it(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        saved_card: Card,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        card_id = saved_card.id
        await grade(dispatcher, bot, card_id, ReviewRating.GOOD, update_id=500)
        await grade(dispatcher, bot, card_id, ReviewRating.GOOD, update_id=510)

        async with session_factory() as session:
            streak = (await session.scalars(select(Streak))).one()
        assert streak.current == 1

    async def test_a_new_learner_gets_one_freeze(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        saved_card: Card,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await grade(dispatcher, bot, saved_card.id, ReviewRating.GOOD)

        async with session_factory() as session:
            assert (await session.scalars(select(Streak))).one().freezes == 1

    async def test_a_missed_day_is_absorbed_by_a_freeze_and_reported(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        saved_card: Card,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await grade(dispatcher, bot, saved_card.id, ReviewRating.GOOD, update_id=600)
        await _rewind_streak(session_factory, days=2, freezes=1, current=4)
        recording.clear()

        await grade(dispatcher, bot, saved_card.id, ReviewRating.GOOD, update_id=610)

        text = answer_message(recording)
        assert "Заморозка сохранила серию" in text
        async with session_factory() as session:
            streak = (await session.scalars(select(Streak))).one()
        assert streak.current == 5
        assert streak.freezes == 0

    async def test_a_missed_day_without_a_freeze_breaks_the_streak(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        saved_card: Card,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await grade(dispatcher, bot, saved_card.id, ReviewRating.GOOD, update_id=700)
        await _rewind_streak(session_factory, days=2, freezes=0, current=9)
        recording.clear()

        await grade(dispatcher, bot, saved_card.id, ReviewRating.GOOD, update_id=710)

        assert "Серия прервалась" in answer_message(recording)
        async with session_factory() as session:
            streak = (await session.scalars(select(Streak))).one()
        assert streak.current == 1
        assert streak.longest == 9

    async def test_reaching_a_round_number_uses_the_milestone_bank(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        saved_card: Card,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await grade(dispatcher, bot, saved_card.id, ReviewRating.GOOD, update_id=800)
        await _rewind_streak(session_factory, days=1, freezes=1, current=2)
        recording.clear()

        await grade(dispatcher, bot, saved_card.id, ReviewRating.GOOD, update_id=810)

        owner = await fresh_user(session_factory)
        assert "milestone" in owner.praise_history
        template = BANKS[PraiseCategory.MILESTONE][owner.praise_history["milestone"][0]]
        text = answer_message(recording)
        assert any(chunk in text for chunk in literal_chunks(template))
        assert "🔥 <b>3</b>" in text

    async def test_earning_a_freeze_is_announced(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        saved_card: Card,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await grade(dispatcher, bot, saved_card.id, ReviewRating.GOOD, update_id=900)
        await _rewind_streak(session_factory, days=1, freezes=0, current=6)
        recording.clear()

        await grade(dispatcher, bot, saved_card.id, ReviewRating.GOOD, update_id=910)

        assert "Новая заморозка" in answer_message(recording)


class TestStatsShowTheStreak:
    async def test_stats_include_the_streak_block(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        saved_card: Card,
    ) -> None:
        await grade(dispatcher, bot, saved_card.id, ReviewRating.GOOD, update_id=1000)
        recording.clear()

        await send(dispatcher, bot, "/stats", update_id=1010)

        text = recording.last_text()
        assert "Серия" in text
        assert "Заморозки" in text

    async def test_stats_settle_a_broken_streak_before_showing_it(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        saved_card: Card,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Opening statistics after a gap must not show a stale number."""
        await grade(dispatcher, bot, saved_card.id, ReviewRating.GOOD, update_id=1100)
        await _rewind_streak(session_factory, days=5, freezes=0, current=7)
        recording.clear()

        await send(dispatcher, bot, "/stats", update_id=1110)

        assert "не начата" in recording.last_text()
        async with session_factory() as session:
            assert (await session.scalars(select(Streak))).one().current == 0

    async def test_the_welcome_back_mentions_a_live_streak(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        saved_card: Card,
    ) -> None:
        await grade(dispatcher, bot, saved_card.id, ReviewRating.GOOD, update_id=1200)
        recording.clear()

        await send(dispatcher, bot, "/start", update_id=1210)

        assert "Серия" in recording.last_text()


async def _rewind_streak(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    days: int,
    freezes: int,
    current: int,
) -> None:
    """Put the streak row into a state that would otherwise take days to reach."""
    from eng_remember.utils.time import local_date, utcnow

    async with session_factory() as session:
        streak = (await session.scalars(select(Streak))).one()
        today = local_date("UTC", now=utcnow())
        streak.current = current
        streak.longest = max(streak.longest, current)
        streak.freezes = freezes
        streak.accounted_on = today - timedelta(days=days)
        await session.commit()
