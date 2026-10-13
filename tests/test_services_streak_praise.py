"""The streak and praise services against a real database.

Day boundaries, persistence and the timezone the learner actually lives in — the
things the pure domain tests deliberately leave out.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.config import StreakSettings
from eng_remember.domain.enums import ReviewRating
from eng_remember.domain.gamification import StreakState
from eng_remember.domain.praise import BANKS, PraiseCategory
from eng_remember.models.streak import Streak
from eng_remember.models.user import User
from eng_remember.services.container import Services
from eng_remember.services.praise import PraiseService
from eng_remember.utils.time import utcnow


class TestStreakService:
    async def test_recording_starts_and_persists(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        transition = await services.streak_service.record(user)
        await session.commit()

        assert transition.state.current == 1
        row = (await session.scalars(select(Streak))).one()
        assert row.current == 1
        assert row.accounted_on is not None

    async def test_recording_twice_in_a_day_persists_once(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        await services.streak_service.record(user)
        await services.streak_service.record(user)
        await session.commit()

        row = (await session.scalars(select(Streak))).one()
        assert row.current == 1

    async def test_yesterday_extends_the_streak(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        moment = utcnow()
        await services.streak_service.record(user, now=moment)
        await session.commit()

        transition = await services.streak_service.record(user, now=moment + timedelta(days=1))
        await session.commit()

        assert transition.state.current == 2

    async def test_one_missed_day_is_absorbed_by_the_starter_freeze(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        moment = utcnow()
        await services.streak_service.record(user, now=moment)
        await session.commit()

        # Two days later means exactly one day was skipped.
        transition = await services.streak_service.record(user, now=moment + timedelta(days=2))
        await session.commit()

        assert transition.freezes_used == 1
        assert not transition.lost
        assert transition.state.current == 2
        assert transition.state.freezes == 0

    async def test_a_second_missed_day_breaks_the_streak(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        """One starter freeze covers one day; the next one is not covered."""
        moment = utcnow()
        await services.streak_service.record(user, now=moment)
        await session.commit()

        transition = await services.streak_service.record(user, now=moment + timedelta(days=3))
        await session.commit()

        assert transition.freezes_used == 1
        assert transition.lost
        assert transition.state.current == 1
        assert transition.state.missed_days == 1

        row = (await session.scalars(select(Streak))).one()
        assert row.current == 1

    async def test_reading_settles_the_streak_without_counting_today(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        moment = utcnow()
        await services.streak_service.record(user, now=moment)
        await session.commit()

        transition = await services.streak_service.current(user, now=moment + timedelta(days=10))
        await session.commit()

        assert transition.state.current == 0
        assert not transition.counted
        row = (await session.scalars(select(Streak))).one()
        assert row.current == 0

    async def test_reading_twice_does_not_inflate_missed_days(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        """``roll_forward`` is idempotent, and the service must not undo that."""
        moment = utcnow()
        await services.streak_service.record(user, now=moment)
        await session.commit()
        later = moment + timedelta(days=5)

        first = await services.streak_service.current(user, now=later)
        await session.commit()
        second = await services.streak_service.current(user, now=later)
        await session.commit()

        assert first.state.missed_days == second.state.missed_days
        row = (await session.scalars(select(Streak))).one()
        assert row.missed_days == first.state.missed_days

    async def test_the_day_boundary_follows_the_learners_timezone(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        """A learner in Auckland crosses midnight before one in UTC."""
        user.timezone = "Pacific/Auckland"
        moment = utcnow()
        await services.streak_service.record(user, now=moment)
        await session.commit()

        # Same instant, but already tomorrow in Auckland only if it is late
        # enough in UTC; an hour later is a new day there and not in UTC.
        transition = await services.streak_service.record(user, now=moment + timedelta(hours=1))
        await session.commit()

        from eng_remember.utils.time import local_date

        same_day = local_date("Pacific/Auckland", now=moment) == local_date(
            "Pacific/Auckland", now=moment + timedelta(hours=1)
        )
        expected = 1 if same_day else 2
        assert transition.state.current == expected

    async def test_an_unknown_timezone_does_not_break_the_streak(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        user.timezone = "Mars/Olympus"

        transition = await services.streak_service.record(user)
        await session.commit()

        assert transition.state.current == 1

    async def test_disabling_streaks_writes_no_rows(
        self, session: AsyncSession, user: User, settings: object
    ) -> None:
        from eng_remember.repositories.streaks import StreakRepository
        from eng_remember.services.streak import StreakService

        service = StreakService(
            streaks=StreakRepository(session),
            settings=StreakSettings(enabled=False),
        )

        transition = await service.record(user)
        await session.commit()

        assert transition.state == StreakState()
        assert await session.scalar(select(Streak)) is None


class TestPraiseService:
    async def test_the_pick_is_written_to_the_user_row(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        pick = services.praise_service.pick(user, rating=ReviewRating.GOOD)
        await session.commit()

        session.expunge_all()
        reloaded = (await session.scalars(select(User))).one()
        assert reloaded.praise_history == {"good": [int(pick.key.split(":")[1])]}

    async def test_consecutive_picks_do_not_repeat(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        keys = []
        for _ in range(20):
            pick = services.praise_service.pick(user, rating=ReviewRating.GOOD)
            keys.append(pick.key)
            await session.commit()

        assert len(set(keys)) == len(keys)

    async def test_a_milestone_switches_to_the_milestone_bank(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        pick = services.praise_service.pick(user, rating=ReviewRating.GOOD, milestone=7)

        assert pick.category is PraiseCategory.MILESTONE
        assert pick.text in BANKS[PraiseCategory.MILESTONE]

    async def test_damaged_history_does_not_break_the_next_pick(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        """The column is JSON; a manual edit must not cost the user a review."""
        corrupt: dict[str, object] = {"good": "not-a-list", "easy": None, "hard": ["x", 2]}
        user.praise_history = corrupt  # type: ignore[assignment]
        await session.commit()

        pick = services.praise_service.pick(user, rating=ReviewRating.GOOD)

        assert pick.text in BANKS[PraiseCategory.GOOD]

    async def test_the_service_is_deterministic_with_a_seeded_rng(self) -> None:
        import random

        first = PraiseService(rng=random.Random(42))
        second = PraiseService(rng=random.Random(42))
        user_a = User(telegram_id=1, praise_history={})
        user_b = User(telegram_id=2, praise_history={})

        picks_a = [first.pick(user_a, rating=ReviewRating.GOOD).key for _ in range(5)]
        picks_b = [second.pick(user_b, rating=ReviewRating.GOOD).key for _ in range(5)]

        assert picks_a == picks_b
