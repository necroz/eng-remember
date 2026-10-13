"""Behaviour that differs between PostgreSQL and the SQLite used elsewhere.

SQLite is enough for most of the suite, but three things are its own: it has no
``timestamptz``, no ``json`` type and no enforced foreign keys. Those are exactly
the places where a test that passes locally can still fail in production, so the
suite exercises them against a real PostgreSQL.

Run with ``make test-integration``; without ``TEST_DATABASE_URL`` the module is
skipped.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import eng_remember.models  # noqa: F401  — registers tables on Base.metadata
from eng_remember.config import Settings
from eng_remember.db import Base, create_engine, create_session_factory
from eng_remember.domain.enums import CardState, ReviewRating
from eng_remember.models.card import Card
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.repositories.users import TelegramProfile
from eng_remember.services.container import Services, build_services
from eng_remember.utils.time import utcnow

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture
async def pg_engine() -> AsyncIterator[AsyncEngine]:
    if not DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")

    settings = Settings(_env_file=None, database_url=DATABASE_URL)
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(pg_engine)


@pytest.fixture
async def pg_session(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with pg_session_factory() as session:
        yield session


@pytest.fixture
async def pg_services(pg_session: AsyncSession) -> Services:
    return build_services(pg_session, Settings(_env_file=None, database_url=DATABASE_URL))


@pytest.fixture
async def pg_user(pg_session: AsyncSession) -> User:
    from eng_remember.repositories.users import UserRepository

    user, _ = await UserRepository(pg_session).get_or_create(
        TelegramProfile(telegram_id=100500, first_name="Tester")
    )
    await pg_session.commit()
    return user


async def test_timestamps_come_back_timezone_aware(
    pg_services: Services, pg_session: AsyncSession, pg_user: User
) -> None:
    """``timestamptz`` is the reason this module exists."""
    card = await pg_services.card_service.add_draft(
        pg_user, CardDraft(term="ubiquitous", translation="вездесущий")
    )
    await pg_session.commit()

    reloaded = await pg_services.cards.get(card.id, user_id=pg_user.id)

    assert reloaded is not None
    assert reloaded.due_at.tzinfo is not None
    assert reloaded.created_at.tzinfo is not None
    assert reloaded.schedule.due_at.tzinfo is not None


async def test_the_review_flow_survives_a_real_scheduler(
    pg_services: Services, pg_session: AsyncSession, pg_user: User
) -> None:
    await pg_services.card_service.add_drafts(
        pg_user,
        [CardDraft(term=f"word{i}", translation=f"слово{i}") for i in range(3)],
    )
    await pg_session.commit()

    queue = await pg_services.review_service.build_queue(pg_user)
    assert len(queue.cards) == 3

    for card in queue.cards:
        await pg_services.review_service.answer(pg_user, card, ReviewRating.GOOD)
    await pg_session.commit()

    assert await pg_services.review_service.get_next(pg_user) is None
    assert await pg_services.reviews.count_since(pg_user.id, since=utcnow().replace(hour=0)) == 3


async def test_json_tags_round_trip(
    pg_services: Services, pg_session: AsyncSession, pg_user: User
) -> None:
    card = await pg_services.card_service.add_draft(
        pg_user,
        CardDraft(term="revenue", translation="выручка", tags=("business", "money")),
    )
    await pg_session.commit()
    pg_session.expunge_all()

    reloaded = await pg_services.cards.get(card.id, user_id=pg_user.id)

    assert reloaded is not None
    assert reloaded.tags == ["business", "money"]


async def test_the_state_check_constraint_is_enforced(
    pg_session: AsyncSession, pg_user: User
) -> None:
    """The enum is stored as text with a CHECK; Postgres actually enforces it."""
    with pytest.raises(IntegrityError):
        await pg_session.execute(
            text(
                "INSERT INTO cards (user_id, deck_id, term, translation, due_at, state, "
                "reps, lapses, is_suspended, created_at, updated_at) "
                "VALUES (:uid, 1, 'x', 'y', now(), 'nonsense', 0, 0, false, now(), now())"
            ),
            {"uid": pg_user.id},
        )


async def test_deleting_a_user_removes_its_data(
    pg_services: Services, pg_session: AsyncSession, pg_user: User
) -> None:
    """Cascades rely on the database; SQLite does not enforce them by default."""
    card = await pg_services.card_service.add_draft(
        pg_user, CardDraft(term="run", translation="бежать")
    )
    queue = await pg_services.review_service.build_queue(pg_user)
    await pg_services.review_service.answer(pg_user, queue.cards[0], ReviewRating.GOOD)
    await pg_session.commit()
    deck_id = card.deck_id

    await pg_session.delete(pg_user)
    await pg_session.commit()

    for table, column, value in (
        ("cards", "id", card.id),
        ("decks", "id", deck_id),
        ("reviews", "user_id", pg_user.id),
    ):
        remaining = await pg_session.scalar(
            text(f"SELECT count(*) FROM {table} WHERE {column} = :value"),
            {"value": value},
        )
        assert remaining == 0, f"{table} still has rows"


async def test_the_daily_new_card_budget_uses_real_timestamps(
    pg_services: Services, pg_session: AsyncSession, pg_user: User
) -> None:
    pg_user.new_cards_per_day = 1
    await pg_services.card_service.add_drafts(
        pg_user, [CardDraft(term=f"w{i}", translation=f"с{i}") for i in range(4)]
    )
    await pg_session.commit()

    first = await pg_services.review_service.build_queue(pg_user)
    assert len(first.cards) == 1

    await pg_services.review_service.answer(pg_user, first.cards[0], ReviewRating.GOOD)
    await pg_session.commit()

    second = await pg_services.review_service.build_queue(pg_user)
    assert second.cards == []


async def test_a_card_keeps_its_instant_when_the_timezone_changes(
    pg_services: Services, pg_session: AsyncSession, pg_user: User
) -> None:
    """Shifting the daily boundary must not shift the stored instants."""
    card = await pg_services.card_service.add_draft(
        pg_user, CardDraft(term="run", translation="бежать")
    )
    await pg_session.commit()
    original_due = card.due_at
    card_id = card.id

    pg_user.timezone = "Pacific/Auckland"
    await pg_session.commit()

    reloaded: Card | None = await pg_services.cards.get(card_id, user_id=pg_user.id)
    assert reloaded is not None
    # The instance is already in the identity map; refresh forces the SELECT
    # that proves the value survived the round trip through timestamptz.
    await pg_session.refresh(reloaded)
    await pg_session.refresh(pg_user, attribute_names=["timezone"])

    assert reloaded.due_at == original_due
    assert reloaded.schedule.due_at == original_due
    assert reloaded.state is CardState.LEARNING
    assert pg_user.timezone == "Pacific/Auckland"


async def test_streak_row_round_trips_through_postgres(
    pg_services: Services, pg_session: AsyncSession, pg_user: User
) -> None:
    """``accounted_on`` is a ``date``: SQLite and Postgres both round-trip it,
    but only Postgres enforces the NOT NULL and the unique constraint."""
    transition = await pg_services.streak_service.record(pg_user)
    await pg_session.commit()

    from eng_remember.models.streak import Streak

    row = (await pg_session.scalars(select(Streak))).one()
    assert row.current == transition.state.current == 1
    assert row.accounted_on is not None


async def test_praise_history_survives_the_json_column(
    pg_services: Services, pg_session: AsyncSession, pg_user: User
) -> None:
    """The no-repeat rule depends on this JSON surviving a restart."""
    from eng_remember.domain.enums import ReviewRating

    for _ in range(5):
        pg_services.praise_service.pick(pg_user, rating=ReviewRating.GOOD)
    await pg_session.commit()
    pg_session.expire_all()

    reloaded = (await pg_session.scalars(select(User))).one()
    used = reloaded.praise_history["good"]
    assert len(used) == 5
    assert len(set(used)) == 5


async def test_the_deck_topic_is_stored_and_read_back(
    pg_services: Services, pg_session: AsyncSession, pg_user: User
) -> None:
    deck = await pg_services.card_service.default_deck(pg_user)
    await pg_services.decks.set_topic(deck, "медицина")
    await pg_session.commit()
    pg_session.expire_all()

    from eng_remember.models.deck import Deck

    reloaded = (await pg_session.scalars(select(Deck))).one()
    assert reloaded.topic == "медицина"


async def test_deleting_a_user_also_removes_the_streak(
    pg_services: Services, pg_session: AsyncSession, pg_user: User
) -> None:
    await pg_services.streak_service.record(pg_user)
    await pg_session.commit()

    await pg_session.delete(pg_user)
    await pg_session.commit()

    from eng_remember.models.streak import Streak

    assert (await pg_session.scalars(select(Streak))).all() == []


async def test_the_reminder_tick_works_against_postgres(
    pg_services: Services, pg_session: AsyncSession, pg_user: User
) -> None:
    """The candidate query mixes a date comparison, EXISTS and a time column —
    the parts where SQLite is most likely to be quietly more forgiving."""
    from eng_remember.services.reminders import build_reminder_service
    from tests.test_services_reminders import RecordingNotifier, give_streak, give_work, moment

    await give_work(pg_services, pg_user, due=2)
    await give_streak(pg_services, pg_session, pg_user, days=4)

    notifier = RecordingNotifier()
    service = build_reminder_service(pg_session, pg_services.settings, notifier)

    morning = await service.run(now=moment(9, 0))
    await pg_session.commit()
    assert morning.sent_count == 1

    evening = await service.run(now=moment(20, 0))
    await pg_session.commit()
    assert evening.sent_count == 1
    assert notifier.sent[-1].kind.value == "streak"

    pg_session.expire_all()
    reloaded = (await pg_session.scalars(select(User))).one()
    assert reloaded.last_reminder_on is not None
    assert reloaded.last_evening_reminder_on is not None


async def test_reminder_time_survives_the_postgres_time_column(
    pg_services: Services, pg_session: AsyncSession, pg_user: User
) -> None:
    """``time without time zone`` round-trips as a naive clock, which is what the
    comparison against the learner's local clock expects."""
    from datetime import time

    pg_user.reminder_time = time(6, 45)
    pg_user.evening_reminder_time = time(21, 15)
    await pg_session.commit()
    pg_session.expire_all()

    reloaded = (await pg_session.scalars(select(User))).one()
    assert reloaded.reminder_time == time(6, 45)
    assert reloaded.evening_reminder_time == time(21, 15)


async def test_the_llm_quota_window_uses_real_timestamps(
    pg_session: AsyncSession, pg_user: User
) -> None:
    """The budget gate counts only what is inside the rolling window.

    ``llm_calls.created_at`` is ``timestamptz`` against an aware cutoff — the
    comparison SQLite is most forgiving about. Rows older than the window must
    not count towards the limit, and fresh ones must.
    """
    from datetime import timedelta

    from eng_remember.config import LlmSettings
    from eng_remember.errors import LlmQuotaExceededError
    from eng_remember.models.llm_call import LlmCall
    from eng_remember.repositories.llm_calls import LlmCallRepository
    from eng_remember.services.llm.quota import QuotaLimitedClient
    from eng_remember.services.llm.schema import ParseResponse
    from tests.test_llm_quota import RecordingModel

    now = utcnow()
    recent = [
        LlmCall(
            user_id=pg_user.id,
            purpose="parse_message",
            provider_model="m",
            created_at=now - timedelta(minutes=5),
        )
        for _ in range(2)
    ]
    stale = [
        LlmCall(
            user_id=pg_user.id,
            purpose="parse_message",
            provider_model="m",
            created_at=now - timedelta(days=2),
        )
        for _ in range(3)
    ]
    pg_session.add_all([*recent, *stale])
    await pg_session.commit()

    async def call(limit: int) -> None:
        client = QuotaLimitedClient(
            RecordingModel(),
            calls=LlmCallRepository(pg_session),
            settings=LlmSettings(max_calls_per_hour=limit, max_tokens_per_day=0),
        )
        await client.complete_json(
            purpose="parse_message",
            system="system",
            user="user",
            schema=ParseResponse,
            user_id=pg_user.id,
        )

    # Two calls happened within the hour: a limit of two already refuses.
    with pytest.raises(LlmQuotaExceededError):
        await call(2)
    # A limit of three passes, so the two-day-old rows did not leak in.
    await call(3)
