"""The reverse card as the learner uses it: meaning in, English word out."""

from __future__ import annotations

from datetime import timedelta

from aiogram import Bot, Dispatcher
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.models.card import Card
from eng_remember.models.review import Review
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from eng_remember.utils.time import utcnow
from tests.conftest import RecordingSession, make_due
from tests.test_bot_flow import send


async def seed_reverse_due(
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> Card:
    """A pair whose forward side was answered yesterday, reverse due now."""
    forward = await services.card_service.add_draft(
        user, CardDraft(term="run", translation="бежать", examples=("He runs every morning.",))
    )
    twin = await services.cards.find_twin(forward)
    assert twin is not None
    await services.commit()
    await make_due(session_factory)
    async with session_factory() as session:
        await session.execute(
            update(Card)
            .where(Card.id == forward.id)
            .values(
                reps=1,
                last_review_at=utcnow() - timedelta(days=1),
                due_at=utcnow() + timedelta(days=7),
            )
        )
        await session.commit()
    return twin


async def test_the_reverse_front_shows_the_meaning(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_reverse_due(services, user, session_factory)

    await send(dispatcher, bot, "/review", update_id=100)

    sent = recording.last_text()
    assert "Переведите на английский" in sent
    assert "бежать" in sent
    assert "run" not in sent


async def test_the_english_answer_is_graded_and_the_card_revealed(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_reverse_due(services, user, session_factory)
    await send(dispatcher, bot, "/review", update_id=100)

    await send(dispatcher, bot, "Run!", update_id=101, date=utcnow())

    assert any("✅ Верно" in text for text in recording.sent_texts())
    assert any("<b>run</b>" in text for text in recording.sent_texts())
    async with session_factory() as check:
        review = (await check.scalars(select(Review))).one()
        assert review.rating.value == 4  # EASY: exact, fast, no hint
