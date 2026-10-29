"""The reverse card as the learner uses it: meaning in, studied word out."""

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
    *,
    term: str = "run",
    translation: str = "бежать",
    language: str = "eng",
) -> Card:
    """A pair whose forward side was answered yesterday, reverse due now."""
    forward = await services.card_service.add_draft(
        user,
        CardDraft(
            term=term,
            translation=translation,
            language=language,
            examples=(f"{term} in a sentence.",),
        ),
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
    assert "🇬🇧" in sent
    assert "Английский" in sent
    assert "напишите слово на английском" in sent
    assert "Переведите на английский" not in sent
    assert "бежать" in sent
    assert "run" not in sent


async def test_a_french_reverse_front_names_the_language(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_reverse_due(
        services,
        user,
        session_factory,
        term="café",
        translation="кафе",
        language="fra",
    )

    await send(dispatcher, bot, "/review", update_id=100)

    sent = recording.last_text()
    assert "🇫🇷" in sent
    assert "Французский" in sent
    assert "напишите слово на французском" in sent
    assert "кафе" in sent
    assert "café" not in sent


async def test_a_reverse_french_answer_is_graded_as_french(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_reverse_due(
        services,
        user,
        session_factory,
        term="café",
        translation="кафе",
        language="fra",
    )
    await send(dispatcher, bot, "/review", update_id=100)

    await send(dispatcher, bot, "café", update_id=101, date=utcnow())

    assert any("✅ Верно" in text for text in recording.sent_texts())


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
