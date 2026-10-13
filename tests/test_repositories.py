"""Query behaviour of the data access layer."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.domain.enums import CardState, ReviewRating
from eng_remember.models.card import Card
from eng_remember.models.review import Review
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft, CardRepository
from eng_remember.repositories.decks import DeckRepository
from eng_remember.repositories.reviews import ReviewRepository
from eng_remember.repositories.users import TelegramProfile, UserRepository
from eng_remember.utils.time import utcnow
from tests.factories import schedule


@pytest.fixture
def cards(session: AsyncSession) -> CardRepository:
    return CardRepository(session)


@pytest.fixture
def decks(session: AsyncSession) -> DeckRepository:
    return DeckRepository(session)


@pytest.fixture
def reviews(session: AsyncSession) -> ReviewRepository:
    return ReviewRepository(session)


@pytest.fixture
async def deck(session: AsyncSession, decks: DeckRepository, user: User) -> int:
    default = await decks.get_or_create_default(user.id, name="Основная колода")
    await session.commit()
    return default.id


async def add(
    cards: CardRepository,
    session: AsyncSession,
    user: User,
    deck_id: int,
    term: str,
    *,
    due_in_days: float = 0.0,
    state: CardState = CardState.LEARNING,
    reps: int = 0,
    lapses: int = 0,
    source: str = "manual",
) -> Card:
    card = await cards.add(
        user_id=user.id,
        deck_id=deck_id,
        draft=CardDraft(term=term, translation=f"{term} по-русски"),
        schedule=schedule(due_in_days, state=state, reps=reps, lapses=lapses),
        source=source,
    )
    await session.commit()
    return card


class TestUsers:
    async def test_first_contact_creates_the_user(self, session: AsyncSession) -> None:
        repository = UserRepository(session)
        user, created = await repository.get_or_create(
            TelegramProfile(telegram_id=777, first_name="Ann", username="ann")
        )

        assert created
        assert user.telegram_id == 777

    async def test_second_contact_returns_the_same_row(self, session: AsyncSession) -> None:
        repository = UserRepository(session)
        first, _ = await repository.get_or_create(TelegramProfile(telegram_id=777))
        second, created = await repository.get_or_create(
            TelegramProfile(telegram_id=777, first_name="Renamed", username="new")
        )

        assert not created
        assert second.id == first.id
        assert second.first_name == "Renamed"

    async def test_username_is_refreshed_on_every_contact(self, session: AsyncSession) -> None:
        repository = UserRepository(session)
        await repository.get_or_create(TelegramProfile(telegram_id=777, username="old"))
        updated, _ = await repository.get_or_create(
            TelegramProfile(telegram_id=777, username="new")
        )

        assert updated.username == "new"


class TestDecks:
    async def test_default_deck_is_created_once(
        self, session: AsyncSession, decks: DeckRepository, user: User
    ) -> None:
        first = await decks.get_or_create_default(user.id, name="Main")
        second = await decks.get_or_create_default(user.id, name="Main")

        assert first.id == second.id
        assert first.is_default
        await session.commit()

    async def test_a_deck_with_the_wanted_name_becomes_the_default(
        self, session: AsyncSession, decks: DeckRepository, user: User
    ) -> None:
        created = await decks.create(user.id, "Main")
        await session.commit()

        default = await decks.get_or_create_default(user.id, name="Main")

        assert default.id == created.id
        assert default.is_default

    async def test_decks_are_scoped_to_their_owner(
        self, session: AsyncSession, decks: DeckRepository, user: User
    ) -> None:
        other, _ = await UserRepository(session).get_or_create(TelegramProfile(telegram_id=999))
        deck = await decks.create(other.id, "Not yours")
        await session.commit()

        assert await decks.get(deck.id, user_id=user.id) is None


class TestCards:
    async def test_term_lookup_ignores_case_and_padding(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        await add(cards, session, user, deck, "ubiquitous")

        assert await cards.find_by_term(user.id, "  UBIQUITOUS ") is not None

    async def test_existing_terms_is_case_insensitive(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        await add(cards, session, user, deck, "Run")

        found = await cards.existing_terms(user.id, ["run", "jump"])

        assert found == {"run"}

    async def test_card_lookup_is_scoped_to_the_owner(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        card = await add(cards, session, user, deck, "run")
        other, _ = await UserRepository(session).get_or_create(TelegramProfile(telegram_id=999))
        await session.commit()

        assert await cards.get(card.id, user_id=user.id) is not None
        assert await cards.get(card.id, user_id=other.id) is None

    async def test_due_queue_holds_scheduled_cards_only(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        await add(cards, session, user, deck, "due", due_in_days=-1, reps=3, state=CardState.REVIEW)
        await add(
            cards, session, user, deck, "later", due_in_days=2, reps=3, state=CardState.REVIEW
        )
        await add(cards, session, user, deck, "brand-new")

        due = await cards.list_due(user.id, now=utcnow(), limit=10)

        assert [card.term for card in due] == ["due"]

    async def test_suspended_cards_leave_the_queue(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        card = await add(cards, session, user, deck, "due", due_in_days=-1, reps=3)
        await cards.set_suspended(card, suspended=True)
        await session.commit()

        assert await cards.list_due(user.id, now=utcnow(), limit=10) == []
        assert await cards.count_due(user.id, now=utcnow()) == 0

    async def test_new_queue_holds_unanswered_cards(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        await add(cards, session, user, deck, "due", due_in_days=-1, reps=3)
        await add(cards, session, user, deck, "brand-new")

        new = await cards.list_new(user.id, now=utcnow(), limit=10)

        assert [card.term for card in new] == ["brand-new"]

    async def test_new_queue_honours_a_zero_limit(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        await add(cards, session, user, deck, "brand-new")

        assert await cards.list_new(user.id, now=utcnow(), limit=0) == []

    async def test_next_due_at_ignores_past_and_unseen_cards(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        await add(cards, session, user, deck, "overdue", due_in_days=-3, reps=3)
        await add(cards, session, user, deck, "brand-new")
        soonest = await add(cards, session, user, deck, "soon", due_in_days=2, reps=3)
        await add(cards, session, user, deck, "much-later", due_in_days=9, reps=3)

        found = await cards.next_due_at(user.id, now=utcnow())

        assert found is not None
        assert abs((found - soonest.due_at).total_seconds()) < 1

    async def test_counts_split_due_rotation_and_new(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        await add(cards, session, user, deck, "due", due_in_days=-1, reps=1)
        await add(
            cards, session, user, deck, "later", due_in_days=5, reps=1, state=CardState.REVIEW
        )
        await add(cards, session, user, deck, "new")

        assert await cards.count_due(user.id, now=utcnow()) == 1
        assert await cards.count_new(user.id, now=utcnow()) == 1
        assert await cards.count_for_user(user.id) == 3

    async def test_counts_group_by_deck(
        self,
        cards: CardRepository,
        session: AsyncSession,
        decks: DeckRepository,
        user: User,
        deck: int,
    ) -> None:
        second = await decks.create(user.id, "Вторая")
        await add(cards, session, user, deck, "one")
        await add(cards, session, user, second.id, "two")
        await add(cards, session, user, second.id, "three")

        assert await cards.count_by_deck(user.id) == {deck: 1, second.id: 2}

    async def test_drafts_are_trimmed_on_save(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        card = await cards.add(
            user_id=user.id,
            deck_id=deck,
            draft=CardDraft(term="  run  ", translation="  бежать ", tags=(" it ", "")),
            schedule=schedule(),
        )

        assert card.term == "run"
        assert card.translation == "бежать"
        assert card.tags == ["it"]

    async def test_delete_all_clears_only_the_given_deck(
        self,
        cards: CardRepository,
        session: AsyncSession,
        decks: DeckRepository,
        user: User,
        deck: int,
    ) -> None:
        second = await decks.create(user.id, "Вторая")
        await add(cards, session, user, deck, "keep")
        await add(cards, session, user, second.id, "drop")

        removed = await cards.delete_all(user.id, deck_id=second.id)
        await session.commit()

        assert removed == 1
        assert await cards.count_for_user(user.id) == 1


class TestReviewLog:
    async def test_reviews_are_counted_from_a_moment(
        self,
        reviews: ReviewRepository,
        session: AsyncSession,
        user: User,
        deck: int,
        cards: CardRepository,
    ) -> None:
        card = await add(cards, session, user, deck, "run")
        now = utcnow()
        await reviews.add(
            Review(
                card_id=card.id,
                user_id=user.id,
                rating=ReviewRating.GOOD,
                reviewed_at=now - timedelta(hours=2),
                state_before=CardState.LEARNING,
                state_after=CardState.LEARNING,
            )
        )
        await reviews.add(
            Review(
                card_id=card.id,
                user_id=user.id,
                rating=ReviewRating.GOOD,
                reviewed_at=now - timedelta(minutes=5),
                state_before=CardState.LEARNING,
                state_after=CardState.REVIEW,
            )
        )
        await session.commit()

        assert await reviews.count_since(user.id, since=now - timedelta(hours=1)) == 1

    async def test_a_card_answered_twice_today_counts_as_one_introduction(
        self,
        reviews: ReviewRepository,
        cards: CardRepository,
        session: AsyncSession,
        user: User,
        deck: int,
    ) -> None:
        card = await add(cards, session, user, deck, "run")
        now = utcnow()
        for minutes_ago in (50, 10):
            await reviews.add(
                Review(
                    card_id=card.id,
                    user_id=user.id,
                    rating=ReviewRating.GOOD,
                    reviewed_at=now - timedelta(minutes=minutes_ago),
                    state_before=CardState.LEARNING,
                    state_after=CardState.LEARNING,
                )
            )
        await session.commit()

        introduced = await reviews.count_cards_introduced_since(
            user.id, since=now - timedelta(hours=1)
        )

        assert introduced == 1

    async def test_a_card_seen_before_today_is_not_a_new_introduction(
        self,
        reviews: ReviewRepository,
        cards: CardRepository,
        session: AsyncSession,
        user: User,
        deck: int,
    ) -> None:
        card = await add(cards, session, user, deck, "run")
        now = utcnow()
        for hours_ago in (30, 1):
            await reviews.add(
                Review(
                    card_id=card.id,
                    user_id=user.id,
                    rating=ReviewRating.GOOD,
                    reviewed_at=now - timedelta(hours=hours_ago),
                    state_before=CardState.LEARNING,
                    state_after=CardState.LEARNING,
                )
            )
        await session.commit()

        introduced = await reviews.count_cards_introduced_since(
            user.id, since=now - timedelta(hours=12)
        )

        assert introduced == 0

    async def test_history_is_newest_first(
        self,
        reviews: ReviewRepository,
        cards: CardRepository,
        session: AsyncSession,
        user: User,
        deck: int,
    ) -> None:
        card = await add(cards, session, user, deck, "run")
        now = utcnow()
        for days_ago in (3, 1):
            await reviews.add(
                Review(
                    card_id=card.id,
                    user_id=user.id,
                    rating=ReviewRating.HARD,
                    reviewed_at=now - timedelta(days=days_ago),
                    state_before=CardState.REVIEW,
                    state_after=CardState.REVIEW,
                )
            )
        await session.commit()

        history = await reviews.list_for_card(card.id)

        assert len(history) == 2
        assert history[0].reviewed_at > history[1].reviewed_at
