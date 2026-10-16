"""The reminder tick: choosing recipients, delivering, and dealing with failures."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.config import Settings
from eng_remember.domain.enums import CardState, ReviewRating
from eng_remember.domain.gamification import StreakState
from eng_remember.domain.reminders import ReminderKind
from eng_remember.errors import RecipientUnreachable
from eng_remember.models.card import Card
from eng_remember.models.review import Review
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.repositories.users import TelegramProfile
from eng_remember.services.container import Services
from eng_remember.services.reminders import ReminderPlan, ReminderService, build_reminder_service
from eng_remember.utils.time import local_date, utcnow

# Cards are created through the services, which stamp them with the real clock,
# so the mocked moments are anchored to today rather than to a fixed date: with a
# hardcoded moment the "due yesterday" fixture would sit in the future and every
# test would silently exercise the empty-queue path.
TODAY = utcnow().date()
TEST_DAY = TODAY


def moment(hour: int, minute: int = 0, *, days: int = 0) -> datetime:
    day = TODAY + timedelta(days=days)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


class RecordingNotifier:
    """A notifier that remembers what it was asked to send, and can refuse."""

    def __init__(self) -> None:
        self.sent: list[ReminderPlan] = []
        self.unreachable: set[int] = set()
        self.broken: set[int] = set()

    async def send_reminder(self, plan: ReminderPlan) -> None:
        if plan.user.id in self.unreachable:
            raise RecipientUnreachable("bot was blocked by the user")
        if plan.user.id in self.broken:
            raise ConnectionError("network is down")
        self.sent.append(plan)


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def reminders(
    services: Services,
    session: AsyncSession,
    settings: Settings,
    notifier: RecordingNotifier,
) -> ReminderService:
    return build_reminder_service(session, settings, notifier)


async def give_work(services: Services, user: User, *, due: int = 1, new: int = 0) -> None:
    from tests.factories import schedule

    deck = await services.card_service.default_deck(user)
    for index in range(due):
        await services.cards.add(
            user_id=user.id,
            deck_id=deck.id,
            draft=CardDraft(term=f"due{index}", translation="повтор"),
            schedule=schedule(-1, state=CardState.REVIEW, reps=3),
        )
    for index in range(new):
        await services.cards.add(
            user_id=user.id,
            deck_id=deck.id,
            draft=CardDraft(term=f"new{index}", translation="новое"),
            schedule=schedule(0),
        )
    await services.commit()


async def give_streak(
    services: Services,
    session: AsyncSession,
    user: User,
    *,
    days: int,
) -> None:
    """A streak that was last counted yesterday, so today is still open.

    Answering a card today would count today and silence the warning, which is
    exactly what the tests about yesterday's streak must avoid.
    """
    streak = await services.streaks.get_or_create(
        user.id, initial=StreakState(current=days, longest=days, freezes=1)
    )
    streak.accounted_on = local_date(user.timezone) - timedelta(days=1)
    await session.commit()


class TestWhoGetsReminded:
    async def test_a_learner_with_work_gets_the_morning_reminder(
        self,
        reminders: ReminderService,
        services: Services,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        await give_work(services, user, due=3)

        run = await reminders.run(now=moment(9, 0))

        assert run.sent_count == 1
        assert notifier.sent[0].due_now == 3
        assert notifier.sent[0].kind.value == "morning"
        assert notifier.sent[0].local_day == TEST_DAY

    async def test_before_the_hour_nothing_is_sent(
        self,
        reminders: ReminderService,
        services: Services,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        await give_work(services, user)

        run = await reminders.run(now=moment(7, 30))

        assert run.sent_count == 0

    async def test_the_same_learner_is_not_reminded_twice_in_a_day(
        self,
        reminders: ReminderService,
        services: Services,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        await give_work(services, user)

        await reminders.run(now=moment(9, 0))
        second = await reminders.run(now=moment(11, 0))

        assert len(notifier.sent) == 1
        assert second.sent_count == 0

    async def test_the_next_day_reminds_again(
        self,
        reminders: ReminderService,
        services: Services,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        await give_work(services, user)
        await reminders.run(now=moment(9, 0))

        run = await reminders.run(now=moment(9, 0, days=1))

        assert run.sent_count == 1

    async def test_the_evening_warning_follows_the_morning_one(
        self,
        reminders: ReminderService,
        services: Services,
        session: AsyncSession,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        await give_work(services, user)
        await give_streak(services, session, user, days=5)
        await reminders.run(now=moment(9, 0))

        run = await reminders.run(now=moment(20, 0))

        assert run.sent_count == 1
        assert notifier.sent[-1].kind.value == "evening"
        assert notifier.sent[-1].streak_days == 5
        assert notifier.sent[-1].streak_warning is True

    async def test_without_a_streak_the_remainder_goes_without_a_warning(
        self,
        reminders: ReminderService,
        services: Services,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        """A learner who never answered has no streak to lose — but the
        evening slot still carries whatever is left in the queue."""
        await give_work(services, user)
        await reminders.run(now=moment(9, 0))

        run = await reminders.run(now=moment(20, 0))

        assert run.sent_count == 1
        assert notifier.sent[-1].kind.value == "evening"
        assert notifier.sent[-1].streak_warning is False

    async def test_answering_the_last_card_silences_the_evening(
        self,
        reminders: ReminderService,
        services: Services,
        session: AsyncSession,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        await give_work(services, user, due=1)
        await reminders.run(now=moment(9, 0))
        queue = await services.review_service.build_queue(user)
        await services.review_service.answer(user, queue.cards[0], ReviewRating.GOOD)
        await session.commit()

        run = await reminders.run(now=moment(20, 0))

        assert run.sent_count == 0

    async def test_a_brand_new_learner_without_cards_is_left_alone(
        self, reminders: ReminderService, user: User, notifier: RecordingNotifier
    ) -> None:
        """No cards, nothing to remind about — and no idea what the bot is for yet."""
        run = await reminders.run(now=moment(9, 0))

        assert run.sent_count == 0

    async def test_the_kill_switch_stops_the_tick(
        self,
        services: Services,
        session: AsyncSession,
        settings: Settings,
        notifier: RecordingNotifier,
        user: User,
    ) -> None:
        await give_work(services, user)
        settings.reminders.enabled = False
        service = build_reminder_service(session, settings, notifier)

        run = await service.run(now=moment(9, 0))

        assert run.candidates == 0
        assert run.sent_count == 0


class TestLearnerSettings:
    async def test_a_chosen_time_is_respected(
        self,
        reminders: ReminderService,
        services: Services,
        session: AsyncSession,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        await give_work(services, user)
        user.reminder_time = time(21, 30)
        await session.commit()

        early = await reminders.run(now=moment(9, 0))
        late = await reminders.run(now=moment(21, 30))

        assert early.sent_count == 0
        assert late.sent_count == 1

    async def test_reminders_can_be_switched_off_per_learner(
        self,
        reminders: ReminderService,
        services: Services,
        session: AsyncSession,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        await give_work(services, user)
        user.reminders_enabled = False
        await session.commit()

        run = await reminders.run(now=moment(9, 0))

        assert run.sent_count == 0

    async def test_the_evening_warning_can_be_switched_off(
        self,
        reminders: ReminderService,
        services: Services,
        session: AsyncSession,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        await give_work(services, user)
        user.evening_reminder = False
        await session.commit()

        await reminders.run(now=moment(9, 0))
        evening = await reminders.run(now=moment(20, 0))

        assert evening.sent_count == 0

    async def test_the_learner_clock_decides(
        self,
        reminders: ReminderService,
        services: Services,
        session: AsyncSession,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        """17:00 UTC is 19:00 in Moscow, so a 09:00 slot is long past there."""
        await give_work(services, user)
        user.timezone = "Europe/Moscow"
        await session.commit()

        run = await reminders.run(now=moment(6, 0))

        assert run.sent_count == 1
        assert notifier.sent[0].local_day == TEST_DAY


class TestDelivery:
    async def test_a_blocked_bot_is_retired_not_retried(
        self,
        reminders: ReminderService,
        services: Services,
        session: AsyncSession,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        await give_work(services, user)
        notifier.unreachable.add(user.id)

        run = await reminders.run(now=moment(9, 0))
        await session.commit()

        assert run.unreachable == [user.id]
        assert run.sent_count == 0
        reloaded = (await session.scalars(select(User))).one()
        assert reloaded.is_blocked

    async def test_an_unreachable_account_is_not_tried_again_today(
        self,
        reminders: ReminderService,
        services: Services,
        session: AsyncSession,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        await give_work(services, user)
        notifier.unreachable.add(user.id)
        await reminders.run(now=moment(9, 0))
        await session.commit()

        second = await reminders.run(now=moment(11, 0))

        assert second.candidates == 0

    async def test_a_transport_failure_is_retried_on_the_next_tick(
        self,
        reminders: ReminderService,
        services: Services,
        session: AsyncSession,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        """The opposite of the blocked case: no watermark, so it comes back."""
        await give_work(services, user)
        notifier.broken.add(user.id)

        first = await reminders.run(now=moment(9, 0))
        await session.commit()
        notifier.broken.clear()
        second = await reminders.run(now=moment(9, 10))

        assert first.failed == [user.id]
        assert second.sent_count == 1

    async def test_a_failure_does_not_stop_the_rest_of_the_batch(
        self,
        reminders: ReminderService,
        services: Services,
        session: AsyncSession,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        await give_work(services, user)
        other, _ = await services.users.get_or_create(TelegramProfile(telegram_id=200200))
        await give_work(services, other)
        await session.commit()
        notifier.broken.add(user.id)

        run = await reminders.run(now=moment(9, 0))

        assert run.failed == [user.id]
        assert [plan.user.id for plan in notifier.sent] == [other.id]

    async def test_the_tick_terminates_even_when_every_delivery_fails(
        self,
        reminders: ReminderService,
        services: Services,
        session: AsyncSession,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        """A permanent failure must not make the cursor walk in place forever."""
        await give_work(services, user)
        notifier.broken.add(user.id)

        run = await reminders.run(now=moment(9, 0))

        assert run.candidates == 1
        assert run.failed == [user.id]


class TestBatching:
    async def test_many_learners_are_handled_in_one_tick(
        self,
        reminders: ReminderService,
        services: Services,
        session: AsyncSession,
        notifier: RecordingNotifier,
    ) -> None:
        for index in range(5):
            learner, _ = await services.users.get_or_create(
                TelegramProfile(telegram_id=300000 + index)
            )
            await give_work(services, learner)
        await session.commit()

        run = await reminders.run(now=moment(9, 0))

        assert run.sent_count == 5
        assert len({plan.user.id for plan in notifier.sent}) == 5

    async def test_a_second_tick_re_examines_but_sends_nothing(
        self, reminders: ReminderService, services: Services, session: AsyncSession, user: User
    ) -> None:
        """The candidate filter is a superset by design.

        Narrowing it to "not yet reminded today" needs each learner's timezone,
        which SQL cannot express portably, so the decision step does that part.
        The trade-off is a redundant look per tick; what must hold is that no
        second message goes out.
        """
        await give_work(services, user)
        await reminders.run(now=moment(9, 0))
        await session.commit()

        second = await reminders.run(now=moment(9, 30))

        assert second.sent_count == 0
        assert second.candidates == 1


class TestDigestPlanning:
    async def test_a_digest_plan_carries_the_week(
        self,
        services: Services,
        session: AsyncSession,
        settings: Settings,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        settings.reminders.digest_enabled = True
        settings.reminders.digest_weekday = TODAY.weekday()
        service = build_reminder_service(session, settings, notifier)
        await give_work(services, user, due=2)
        await give_streak(services, session, user, days=4)
        card = (await services.cards.list_for_user(user.id, limit=1))[0]
        card.again_count = 3
        await services.reviews.add(
            Review(
                card_id=card.id,
                user_id=user.id,
                rating=ReviewRating.GOOD,
                reviewed_at=moment(12, 0, days=-1),
                state_before=CardState.REVIEW,
                state_after=CardState.REVIEW,
            )
        )
        await session.execute(
            update(Card).where(Card.id == card.id).values(created_at=moment(12, 0, days=-1))
        )
        await session.commit()

        run = await service.run(now=moment(9, 0))

        assert run.sent_count == 1
        plan = notifier.sent[-1]
        assert plan.kind is ReminderKind.DIGEST
        digest = plan.digest
        assert digest is not None
        assert digest.total == 1
        assert digest.correct == 1
        assert digest.new_words == 1
        assert len(digest.days) == 28
        assert digest.weak == ((card.term, 3),)
        assert digest.due_now == 2

    async def test_counts_advertise_words_not_pairs(
        self,
        services: Services,
        session: AsyncSession,
        settings: Settings,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        """A pair is introduced one side a day: «новых» must promise words."""
        await services.card_service.add_drafts(
            user,
            [
                CardDraft(term="run", translation="бежать"),
                CardDraft(term="swim", translation="плавать"),
                CardDraft(term="fly", translation="летать"),
            ],
        )
        await session.commit()

        run = await build_reminder_service(session, settings, notifier).run(
            now=moment(9, 0, days=1)
        )

        assert run.sent_count == 1
        assert notifier.sent[-1].new_available == 3

    async def test_the_digest_counts_words_not_pairs(
        self,
        services: Services,
        session: AsyncSession,
        settings: Settings,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        settings.reminders.digest_enabled = True
        settings.reminders.digest_weekday = TODAY.weekday()
        service = build_reminder_service(session, settings, notifier)
        outcome = await services.card_service.add_drafts(
            user, [CardDraft(term="run", translation="бежать")]
        )
        forward = outcome.added[0]
        twin = await services.cards.find_twin(forward)
        assert twin is not None
        forward.again_count = 4
        twin.again_count = 2
        await session.execute(update(Card).values(created_at=moment(12, 0, days=-1)))
        await session.commit()

        run = await service.run(now=moment(9, 0))

        assert run.sent_count == 1
        digest = notifier.sent[-1].digest
        assert digest is not None
        assert digest.new_words == 1
        assert digest.weak == (("run", 4),)

    async def test_the_digest_marks_both_watermarks(
        self,
        services: Services,
        session: AsyncSession,
        settings: Settings,
        user: User,
        notifier: RecordingNotifier,
    ) -> None:
        settings.reminders.digest_enabled = True
        settings.reminders.digest_weekday = TODAY.weekday()
        service = build_reminder_service(session, settings, notifier)
        await give_work(services, user)

        await service.run(now=moment(9, 0))
        await session.commit()
        session.expire_all()

        reloaded = (await session.scalars(select(User))).one()
        assert reloaded.last_digest_on == TODAY
        assert reloaded.last_reminder_on == TODAY
