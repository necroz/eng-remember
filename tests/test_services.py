"""Use cases on top of the repositories."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.domain.enums import CardState, ReviewRating
from eng_remember.errors import DuplicateCardError
from eng_remember.models.card import Card
from eng_remember.models.review import Review
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.repositories.users import TelegramProfile
from eng_remember.services.container import Services
from eng_remember.utils.time import utcnow
from tests.factories import schedule


@pytest.fixture
def draft() -> CardDraft:
    return CardDraft(term="ubiquitous", translation="вездесущий", transcription="/juːˈbɪkwɪtəs/")


class TestCardService:
    async def test_first_card_creates_the_default_deck(
        self, services: Services, user: User, draft: CardDraft
    ) -> None:
        card = await services.card_service.add_draft(user, draft, source="manual")

        deck = await services.decks.get_default(user.id)
        assert deck is not None
        assert card.deck_id == deck.id
        assert card.term == "ubiquitous"

    async def test_a_word_added_today_is_not_in_todays_queue(
        self, services: Services, user: User, draft: CardDraft
    ) -> None:
        """The day a word is added is not a day it is drilled on."""
        moment = datetime(2026, 5, 14, 21, 30, tzinfo=UTC)

        card = await services.card_service.add_draft(user, draft, now=moment)

        assert card.reps == 0
        assert card.state is CardState.LEARNING
        assert card.due_at == datetime(2026, 5, 15, 0, 0, tzinfo=UTC)
        assert not card.schedule.is_due(moment)
        queue = await services.review_service.build_queue(user, now=moment)
        assert queue.cards == []

    async def test_the_word_is_waiting_the_next_day(
        self, services: Services, user: User, draft: CardDraft
    ) -> None:
        moment = datetime(2026, 5, 14, 21, 30, tzinfo=UTC)
        card = await services.card_service.add_draft(user, draft, now=moment)

        queue = await services.review_service.build_queue(user, now=card.due_at)

        assert [queued.id for queued in queue.cards] == [card.id]

    async def test_the_next_day_is_the_learners_midnight(
        self, services: Services, user: User, draft: CardDraft
    ) -> None:
        """Berlin's tomorrow can still be today in UTC — the learner's zone wins."""
        user.timezone = "Europe/Berlin"
        moment = datetime(2026, 5, 14, 21, 30, tzinfo=UTC)  # 23:30 in Berlin

        card = await services.card_service.add_draft(user, draft, now=moment)

        assert card.due_at == datetime(2026, 5, 14, 22, 0, tzinfo=UTC)

    async def test_the_wait_survives_the_dst_day_that_lasts_25_hours(
        self, services: Services, user: User, draft: CardDraft
    ) -> None:
        """Berlin leaves daylight saving on 25 October 2026; that local day is 25 hours long.

        A word added just after its midnight is in the past a day later by the
        clock — and a due date in the past is a card back in today's queue.
        """
        user.timezone = "Europe/Berlin"
        moment = datetime(2026, 10, 24, 22, 30, tzinfo=UTC)  # 00:30 CEST on the 25th

        card = await services.card_service.add_draft(user, draft, now=moment)

        assert card.due_at == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)  # 00:00 CET on the 26th
        assert not card.schedule.is_due(moment)

    async def test_the_same_term_twice_is_refused(
        self, services: Services, user: User, draft: CardDraft
    ) -> None:
        await services.card_service.add_draft(user, draft)

        with pytest.raises(DuplicateCardError):
            await services.card_service.add_draft(user, draft)

    async def test_duplicate_detection_ignores_case(
        self, services: Services, user: User, draft: CardDraft
    ) -> None:
        await services.card_service.add_draft(user, draft)
        shouted = CardDraft(term="UBIQUITOUS", translation="вездесущий")

        with pytest.raises(DuplicateCardError):
            await services.card_service.add_draft(user, shouted)

    async def test_a_batch_reports_instead_of_failing(self, services: Services, user: User) -> None:
        await services.card_service.add_draft(user, CardDraft(term="run", translation="бежать"))

        outcome = await services.card_service.add_drafts(
            user,
            [
                CardDraft(term="run", translation="бежать"),
                CardDraft(term="jump", translation="прыгать"),
                CardDraft(term="jump", translation="прыгать"),
                CardDraft(term="swim", translation="плавать"),
            ],
        )

        assert [card.term for card in outcome.added] == ["jump", "swim"]
        assert outcome.skipped_existing == ["run"]
        assert outcome.skipped_in_batch == ["jump"]
        assert outcome.total_skipped == 2

    async def test_an_empty_batch_changes_nothing(self, services: Services, user: User) -> None:
        outcome = await services.card_service.add_drafts(user, [])

        assert outcome.added == []
        assert await services.cards.count_for_user(user.id) == 0

    async def test_cards_can_be_placed_in_a_named_deck(
        self, services: Services, user: User
    ) -> None:
        deck = await services.decks.create(user.id, "Бизнес")

        card = await services.card_service.add_draft(
            user, CardDraft(term="revenue", translation="выручка"), deck=deck
        )

        assert card.deck_id == deck.id

    async def test_an_unknown_deck_falls_back_to_the_default(
        self, services: Services, user: User
    ) -> None:
        deck = await services.card_service.resolve_deck(user, deck_id=999_999)

        assert deck.is_default

    async def test_delete_refuses_another_users_card(
        self, services: Services, session: AsyncSession, user: User, draft: CardDraft
    ) -> None:
        card = await services.card_service.add_draft(user, draft)
        stranger, _ = await services.users.get_or_create(TelegramProfile(telegram_id=999))
        await session.commit()

        assert await services.card_service.delete(stranger, card.id) is None
        assert await services.cards.get(card.id, user_id=user.id) is not None

    async def test_stats_reflect_the_queue(
        self, services: Services, session: AsyncSession, user: User
    ) -> None:
        outcome = await services.card_service.add_drafts(
            user,
            [CardDraft(term=f"word{i}", translation=f"слово{i}") for i in range(3)],
        )
        await session.commit()

        # Added today: in the collection, but not in today's queue.
        today = await services.card_service.stats(user)
        assert today.total_cards == 3
        assert today.new_cards == 0
        assert today.due_now == 0
        assert today.reviews_today == 0

        tomorrow = await services.card_service.stats(user, now=outcome.added[0].due_at)
        assert tomorrow.new_cards == 3


class TestReviewService:
    @pytest.fixture
    async def deck_id(self, services: Services, user: User) -> int:
        deck = await services.card_service.default_deck(user)
        return deck.id

    async def queue_cards(
        self,
        services: Services,
        session: AsyncSession,
        user: User,
        deck_id: int,
        *,
        due: int = 0,
        new: int = 0,
    ) -> None:
        for index in range(due):
            await services.cards.add(
                user_id=user.id,
                deck_id=deck_id,
                draft=CardDraft(term=f"due{index}", translation="повтор"),
                schedule=schedule(-1, state=CardState.REVIEW, reps=3),
            )
        for index in range(new):
            await services.cards.add(
                user_id=user.id,
                deck_id=deck_id,
                draft=CardDraft(term=f"new{index}", translation="новое"),
                schedule=schedule(0),
            )
        await session.commit()

    async def test_due_cards_come_before_new_ones(
        self, services: Services, session: AsyncSession, user: User, deck_id: int
    ) -> None:
        await self.queue_cards(services, session, user, deck_id, due=2, new=2)

        queue = await services.review_service.build_queue(user)

        assert [card.term for card in queue.cards] == ["due0", "due1", "new0", "new1"]
        assert queue.due_total == 2
        assert queue.new_total == 2

    async def test_new_cards_are_capped_by_the_daily_budget(
        self, services: Services, session: AsyncSession, user: User, deck_id: int
    ) -> None:
        user.new_cards_per_day = 2
        await self.queue_cards(services, session, user, deck_id, new=5)

        queue = await services.review_service.build_queue(user)

        assert len(queue.cards) == 2
        assert queue.new_allowance == 2

    async def test_the_daily_budget_shrinks_as_cards_are_introduced(
        self, services: Services, session: AsyncSession, user: User, deck_id: int
    ) -> None:
        user.new_cards_per_day = 1
        await self.queue_cards(services, session, user, deck_id, new=3)

        first = await services.review_service.build_queue(user)
        assert len(first.cards) == 1
        await services.review_service.answer(user, first.cards[0], ReviewRating.GOOD)
        await session.commit()

        second = await services.review_service.build_queue(user)

        assert second.cards == []
        assert second.new_allowance == 0

    async def test_a_due_card_leaves_the_queue_after_being_answered(
        self, services: Services, session: AsyncSession, user: User, deck_id: int
    ) -> None:
        await self.queue_cards(services, session, user, deck_id, due=1)

        queue = await services.review_service.build_queue(user)
        await services.review_service.answer(user, queue.cards[0], ReviewRating.GOOD)
        await session.commit()

        assert await services.review_service.get_next(user) is None

    async def test_answering_writes_a_review_and_moves_the_card(
        self, services: Services, session: AsyncSession, user: User, deck_id: int
    ) -> None:
        await self.queue_cards(services, session, user, deck_id, new=1)
        card = (await services.review_service.build_queue(user)).cards[0]
        before = card.due_at

        result = await services.review_service.answer(user, card, ReviewRating.GOOD)
        await session.commit()

        assert result.card.reps == 1
        assert result.card.last_review_at is not None
        assert result.card.due_at > before
        assert result.interval_seconds > 0
        assert result.interval_text

        count = await session.scalar(select(func.count(Review.id)))
        assert count == 1
        review = (await session.scalars(select(Review))).one()
        assert review.rating is ReviewRating.GOOD
        assert review.state_before is CardState.LEARNING
        assert review.user_id == user.id
        assert review.card_id == card.id

    async def test_the_answer_interval_is_reported(
        self, services: Services, session: AsyncSession, user: User, deck_id: int
    ) -> None:
        await self.queue_cards(services, session, user, deck_id, new=1)
        card = (await services.review_service.build_queue(user)).cards[0]

        easy = await services.review_service.answer(user, card, ReviewRating.EASY)
        await session.commit()

        assert easy.interval_seconds > 60 * 60

    async def test_forgetting_a_reviewed_card_counts_a_lapse(
        self, services: Services, session: AsyncSession, user: User, deck_id: int
    ) -> None:
        await self.queue_cards(services, session, user, deck_id, due=1)
        card = (await services.review_service.build_queue(user)).cards[0]

        result = await services.review_service.answer(user, card, ReviewRating.AGAIN)
        await session.commit()

        assert result.state_before is CardState.REVIEW
        assert result.card.lapses == 1
        assert result.card.state is CardState.RELEARNING

    async def test_forgetting_a_new_card_is_not_a_lapse(
        self, services: Services, session: AsyncSession, user: User, deck_id: int
    ) -> None:
        await self.queue_cards(services, session, user, deck_id, new=1)
        card = (await services.review_service.build_queue(user)).cards[0]

        result = await services.review_service.answer(user, card, ReviewRating.AGAIN)
        await session.commit()

        assert result.card.lapses == 0

    async def test_an_empty_collection_yields_an_empty_queue(
        self, services: Services, user: User
    ) -> None:
        queue = await services.review_service.build_queue(user)

        assert queue.cards == []
        assert not queue
        assert len(queue) == 0
        assert await services.review_service.get_next(user) is None

    async def test_review_timestamps_are_timezone_aware(
        self, services: Services, session: AsyncSession, user: User, deck_id: int
    ) -> None:
        """SQLite drops the offset; the service must not leak a naive datetime."""
        await self.queue_cards(services, session, user, deck_id, new=1)
        card = (await services.review_service.build_queue(user)).cards[0]

        result = await services.review_service.answer(user, card, ReviewRating.GOOD)

        assert result.due_at.tzinfo is not None
        assert abs((result.due_at - utcnow()) - timedelta(minutes=10)) < timedelta(minutes=2)


class TestCardExamples:
    async def test_draft_normalizes_example_and_examples(self) -> None:
        draft = CardDraft(
            term="run", translation="бежать", examples=("He runs.", "A run.")
        ).normalized()

        assert draft.example == "He runs."
        assert draft.examples == ("He runs.", "A run.")

    async def test_new_card_keeps_all_examples(self, services: Services, user: User) -> None:
        card = await services.card_service.add_draft(
            user,
            CardDraft(term="run", translation="бежать", examples=("He runs.", "A run.")),
        )

        assert card.examples == ["He runs.", "A run."]
        assert card.example == "He runs."

    async def test_add_draft_keeps_the_level_fields(self, services: Services, user: User) -> None:
        card = await services.card_service.add_draft(
            user,
            CardDraft(term="run", translation="бежать", cefr="B1", frequency="rare"),
        )

        assert card.cefr == "B1"
        assert card.frequency == "rare"


class TestAgainCount:
    async def test_again_counts_even_while_learning(
        self, services: Services, user: User, saved_card: Card
    ) -> None:
        """lapses ignores failures in learning; leech detection must not."""
        await services.review_service.answer(user, saved_card, ReviewRating.AGAIN)

        assert saved_card.again_count == 1
        assert saved_card.lapses == 0

    async def test_other_ratings_do_not_count(
        self, services: Services, user: User, saved_card: Card
    ) -> None:
        await services.review_service.answer(user, saved_card, ReviewRating.GOOD)

        assert saved_card.again_count == 0
