"""The /paused list and returning words to rotation."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot.callbacks import PauseCB
from eng_remember.models.card import Card
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from tests.conftest import RecordingSession
from tests.test_bot_flow import send, tap


async def seed_card(
    services: Services,
    user: User,
    *,
    term: str,
    translation: str,
    suspended: bool = True,
) -> Card:
    card = await services.card_service.add_draft(
        user, CardDraft(term=term, translation=translation)
    )
    if suspended:
        await services.card_service.suspend(user, card.id)
    await services.commit()
    return card


def last_callback_answer(recording: RecordingSession) -> AnswerCallbackQuery:
    answers = [m for m in recording.requests if isinstance(m, AnswerCallbackQuery)]
    assert answers, recording.requests
    return answers[-1]


async def test_paused_lists_suspended_words(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
) -> None:
    await seed_card(services, user, term="run", translation="бежать")

    await send(dispatcher, bot, "/paused")

    text = recording.last_text()
    assert "run" in text
    assert "бежать" in text
    assert any("ps:resume" in data for data in recording.last_button_data())


async def test_paused_lists_a_word_once(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
) -> None:
    forward = await services.card_service.add_draft(
        user, CardDraft(term="run", translation="бежать")
    )
    await services.card_service.suspend(user, forward.id)
    await services.commit()

    await send(dispatcher, bot, "/paused", update_id=100)

    text = recording.last_text()
    assert text.count("run") == 1
    assert "На паузе 1 слово" in text


async def test_paused_with_nothing_says_so(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
) -> None:
    await send(dispatcher, bot, "/paused")

    assert "ничего нет" in recording.last_text()


async def test_resume_returns_the_card(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    card = await seed_card(services, user, term="run", translation="бежать")
    await send(dispatcher, bot, "/paused")

    await tap(
        dispatcher,
        bot,
        PauseCB(action="resume", card_id=card.id).pack(),
        update_id=10,
    )

    async with session_factory() as check:
        reloaded = await check.get(Card, card.id)
    assert reloaded is not None
    assert reloaded.is_suspended is False
    assert "ничего нет" in recording.last_text()


async def test_resume_of_a_missing_card_alerts(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
) -> None:
    await tap(
        dispatcher,
        bot,
        PauseCB(action="resume", card_id=999_999).pack(),
        update_id=10,
    )

    assert last_callback_answer(recording).show_alert
