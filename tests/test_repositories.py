"""Query behaviour of the data access layer."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import String, Text, event
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from eng_remember.domain.enums import CardDirection, CardState, ReviewRating
from eng_remember.domain.languages import DEFAULT_LANGUAGE
from eng_remember.models.card import Card
from eng_remember.models.review import Review
from eng_remember.models.user import User
from eng_remember.repositories.cards import COLUMN_WIDTHS, CardDraft, CardRepository
from eng_remember.repositories.decks import DeckRepository
from eng_remember.repositories.reviews import ReviewRepository
from eng_remember.repositories.users import TelegramProfile, UserRepository
from eng_remember.utils.time import utcnow
from tests.factories import schedule


@contextmanager
def record_statements(engine: AsyncEngine) -> Iterator[list[str]]:
    """Collect the SQL a block really sends, for query-shape assertions."""
    statements: list[str] = []

    def record(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(statement)

    target = engine.sync_engine
    event.listen(target, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(target, "before_cursor_execute", record)


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
    language: str = DEFAULT_LANGUAGE,
    direction: CardDirection = CardDirection.FORWARD,
) -> Card:
    card = await cards.add(
        user_id=user.id,
        deck_id=deck_id,
        draft=CardDraft(term=term, translation=f"{term} по-русски", language=language),
        schedule=schedule(due_in_days, state=state, reps=reps, lapses=lapses),
        source=source,
        direction=direction,
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

    async def test_touch_is_throttled_to_once_per_five_minutes(
        self, session: AsyncSession, engine: AsyncEngine
    ) -> None:
        """Every update must not rewrite ``last_seen_at``.

        The value only powers "when did we last hear from this learner", so a
        second contact a minute later has nothing to add and an UPDATE per
        keystroke is pure write amplification.
        """
        repository = UserRepository(session)
        user, _ = await repository.get_or_create(TelegramProfile(telegram_id=778))
        await session.commit()
        first = utcnow()

        with record_statements(engine) as statements:
            await repository.touch(user, when=first)
            await session.commit()
            await repository.touch(user, when=first + timedelta(minutes=1))
            await session.commit()

        updates = [sql for sql in statements if sql.lstrip().upper().startswith("UPDATE")]
        assert len(updates) == 1
        assert user.last_seen_at == first

    async def test_touch_updates_again_once_the_window_has_passed(
        self, session: AsyncSession
    ) -> None:
        repository = UserRepository(session)
        user, _ = await repository.get_or_create(TelegramProfile(telegram_id=779))
        await session.commit()
        first = utcnow()
        await repository.touch(user, when=first)
        await session.commit()

        later = first + timedelta(minutes=5)
        await repository.touch(user, when=later)
        await session.commit()

        assert user.last_seen_at == later


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

    async def test_count_for_user_lets_the_database_count(
        self, session: AsyncSession, decks: DeckRepository, user: User, engine: AsyncEngine
    ) -> None:
        """Pulling every id into Python to call ``len`` was busywork."""
        await decks.create(user.id, "One")
        await decks.create(user.id, "Two")
        await session.commit()

        with record_statements(engine) as statements:
            assert await decks.count_for_user(user.id) == 2

        counts = [sql for sql in statements if "count(" in sql.lower()]
        assert counts, statements


class TestUserModel:
    async def test_loading_a_user_does_not_select_decks_or_streak(
        self, session: AsyncSession, engine: AsyncEngine
    ) -> None:
        """The collections are never read; eager loads were pure overhead."""
        repository = UserRepository(session)
        created, _ = await repository.get_or_create(TelegramProfile(telegram_id=9090))
        await session.commit()
        user_id = created.id
        session.expire_all()

        with record_statements(engine) as statements:
            loaded = await session.get(User, user_id)

        assert loaded is not None
        assert len(statements) == 1, statements
        assert "decks" not in statements[0].lower()
        assert "streaks" not in statements[0].lower()

    async def test_the_collections_refuse_implicit_loads(self, session: AsyncSession) -> None:
        repository = UserRepository(session)
        created, _ = await repository.get_or_create(TelegramProfile(telegram_id=9091))
        await session.commit()
        user_id = created.id
        session.expire_all()

        loaded = await session.get(User, user_id)
        assert loaded is not None

        with pytest.raises(InvalidRequestError):
            _ = loaded.decks
        with pytest.raises(InvalidRequestError):
            _ = loaded.streak


def test_normalized_draft_keeps_and_resolves_its_language() -> None:
    assert CardDraft(term=" run ", translation=" бежать ").normalized().language == "eng"
    assert (
        CardDraft(term="bonjour", translation="здравствуйте", language="fr").normalized().language
        == "fra"
    )
    assert (
        CardDraft(term="привет", translation="привет", language="ru").normalized().language == "eng"
    )


# Columns of ``Card`` a draft never sets: the card's origin and direction, and
# its scheduling state. Every other bounded String column a draft can fill has
# to be in ``COLUMN_WIDTHS``; adding one without a width fails this test.
_NON_DRAFT_COLUMNS = frozenset({"source", "state", "direction"})


def test_every_string_column_of_a_draft_has_a_declared_width() -> None:
    """The truncation table must cover every bounded String field a draft fills.

    Text columns are unbounded by design; the three ``_NON_DRAFT_COLUMNS`` are
    set by the repository and the scheduler, not the parser. The rest is exactly
    what ``_fit`` cuts, and the widths must match the model.
    """
    bounded = {
        name: column.type.length
        for name, column in Card.__table__.c.items()
        if isinstance(column.type, String) and not isinstance(column.type, Text)
    }

    assert set(bounded) - set(COLUMN_WIDTHS) == _NON_DRAFT_COLUMNS
    assert {name: bounded[name] for name in COLUMN_WIDTHS} == COLUMN_WIDTHS


def test_normalized_draft_truncates_to_column_widths() -> None:
    draft = CardDraft(
        term="t" * 300,
        translation="я" * 1000,
        transcription="/" + "ɪ" * 200 + "/",
        part_of_speech="adjective" * 10,
        example="e" * 1000,
        notes="n" * 1000,
        cefr="B2",
        frequency="common",
    ).normalized()

    assert draft.term == "t" * 255
    assert len(draft.transcription or "") == 128
    assert len(draft.part_of_speech or "") == 32
    # Text columns have no declared width: their content is kept whole.
    assert draft.translation == "я" * 1000
    assert draft.example == "e" * 1000
    assert draft.notes == "n" * 1000
    assert draft.cefr == "B2"
    assert draft.frequency == "common"


def test_normalized_draft_truncates_every_short_level_field() -> None:
    """The three narrow fields are as easy to overflow as the term."""
    draft = CardDraft(
        term="ubiquitous",
        translation="вездесущий",
        frequency="very common",
        cefr="B2/C1",
        language="portuguese",
    ).normalized()

    assert draft.frequency == "very com"
    assert draft.cefr == "B2"
    assert draft.language == "por"


def test_normalized_draft_drops_the_space_left_by_truncation() -> None:
    draft = CardDraft(term="t" * 254 + " tail", translation="перевод").normalized()

    assert draft.term == "t" * 254


class TestCards:
    async def test_term_lookup_ignores_case_and_padding(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        await add(cards, session, user, deck, "ubiquitous")

        assert await cards.find_by_term(user.id, "  UBIQUITOUS ") is not None

    async def test_a_card_remembers_its_language(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        card = await add(cards, session, user, deck, "bonjour", language="fra")
        card_id = card.id
        user_id = user.id
        session.expire_all()

        reloaded = await cards.get(card_id, user_id=user_id)
        assert reloaded is not None
        assert reloaded.language == "fra"

    async def test_term_lookup_is_scoped_by_language(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        await add(cards, session, user, deck, "hotel", language="fra")

        assert await cards.find_by_term(user.id, "hotel", language="fra") is not None
        assert await cards.find_by_term(user.id, "hotel", language="eng") is None
        assert await cards.find_by_term(user.id, "hotel") is None

    async def test_the_same_term_can_exist_in_two_languages(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        await add(cards, session, user, deck, "hotel", language="eng")
        await add(cards, session, user, deck, "hotel", language="fra")

        listed = await cards.list_for_user(user.id, direction=CardDirection.FORWARD)

        assert sorted((card.term, card.language) for card in listed) == [
            ("hotel", "eng"),
            ("hotel", "fra"),
        ]

    async def test_twin_lookup_is_scoped_by_language(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        french_forward = await add(cards, session, user, deck, "hotel", language="fra")
        french_reverse = await add(
            cards,
            session,
            user,
            deck,
            "hotel",
            language="fra",
            direction=CardDirection.REVERSE,
        )
        english_forward = await add(cards, session, user, deck, "hotel", language="eng")

        assert await cards.find_twin(french_forward) == french_reverse
        assert await cards.find_twin(english_forward) is None

    async def test_existing_terms_is_case_insensitive(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        await add(cards, session, user, deck, "Run")

        found = await cards.existing_terms(user.id, ["run", "jump"])

        assert found == {"run"}

    async def test_existing_terms_are_casefolded(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        await add(cards, session, user, deck, "Straße", language="deu")

        found = await cards.existing_terms(user.id, ["Straße"], language="deu")

        assert found == {"strasse"}

    async def test_known_terms_can_be_filtered_by_language(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        await add(cards, session, user, deck, "chat", language="fra")
        await add(cards, session, user, deck, "dog", language="eng")

        assert await cards.all_terms(user.id, language="fra") == {"chat"}
        assert await cards.all_terms(user.id) == {"chat", "dog"}
        assert await cards.existing_terms(user.id, ["chat"], language="fra") == {"chat"}
        assert await cards.existing_terms(user.id, ["chat"], language="eng") == set()

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

    async def test_next_due_at_counts_a_word_that_waits_for_tomorrow(
        self, cards: CardRepository, session: AsyncSession, user: User, deck: int
    ) -> None:
        """The queue is empty on the day a word is added — say when it fills up."""
        tomorrow = await add(cards, session, user, deck, "tomorrow", due_in_days=1)
        await add(cards, session, user, deck, "later", due_in_days=5, reps=3)

        found = await cards.next_due_at(user.id, now=utcnow())

        assert found is not None
        assert abs((found - tomorrow.due_at).total_seconds()) < 1

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

    async def test_introductions_are_counted_per_learner_in_one_pass(
        self,
        reviews: ReviewRepository,
        cards: CardRepository,
        decks: DeckRepository,
        session: AsyncSession,
        user: User,
        deck: int,
    ) -> None:
        """The reminder tick counts a whole batch against different midnights.

        Each learner gets their own boundary, so one call answers "how many
        introductions today" for everyone without a query per account.
        """
        other, _ = await UserRepository(session).get_or_create(
            TelegramProfile(telegram_id=200200, first_name="Other")
        )
        other_deck = (await decks.get_or_create_default(other.id, name="Основная колода")).id
        now = utcnow()

        async def introduce(learner: User, deck_id: int, term: str, hours_ago: float) -> None:
            card = await add(cards, session, learner, deck_id, term)
            await reviews.add(
                Review(
                    card_id=card.id,
                    user_id=learner.id,
                    rating=ReviewRating.GOOD,
                    reviewed_at=now - timedelta(hours=hours_ago),
                    state_before=CardState.LEARNING,
                    state_after=CardState.LEARNING,
                )
            )

        # One fresh card each, plus a card each learner already knew before
        # their own boundary: only the fresh ones are introductions.
        await introduce(user, deck, "fresh", 0.1)
        await introduce(user, deck, "known", 30)
        await introduce(other, other_deck, "fresh", 0.1)
        await introduce(other, other_deck, "known", 2)
        await session.commit()

        counted = await reviews.introduced_counts(
            [user.id, other.id],
            since_by_user={
                user.id: now - timedelta(hours=12),
                other.id: now - timedelta(hours=1),
            },
        )

        assert counted == {user.id: 1, other.id: 1}

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


class TestSuspendedListing:
    async def test_suspended_cards_are_listed_and_counted(
        self,
        cards: CardRepository,
        session: AsyncSession,
        user: User,
        deck: int,
    ) -> None:
        card = await add(cards, session, user, deck, "ubiquitous")

        await cards.set_suspended(card, suspended=True)
        assert [item.id for item in await cards.list_suspended(user.id)] == [card.id]
        assert await cards.count_suspended(user.id) == 1

        await cards.set_suspended(card, suspended=False)
        assert await cards.list_suspended(user.id) == []
        assert await cards.count_suspended(user.id) == 0
