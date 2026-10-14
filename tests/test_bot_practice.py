"""The practice quiz as the learner uses it."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import InlineKeyboardButton
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot import texts
from eng_remember.domain.enums import CardState
from eng_remember.models.card import Card
from eng_remember.models.review import Review
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from tests.conftest import RecordingSession
from tests.factories import schedule
from tests.test_bot_flow import send, tap


async def seed_seen(
    services: Services,
    user: User,
    *pairs: tuple[str, str],
) -> dict[str, str]:
    """Cards already in rotation, returned as a term -> translation map."""
    deck = await services.card_service.default_deck(user)
    for term, translation in pairs:
        await services.cards.add(
            user_id=user.id,
            deck_id=deck.id,
            draft=CardDraft(term=term, translation=translation, part_of_speech="noun"),
            schedule=schedule(due_in_days=7, state=CardState.REVIEW, reps=1),
            source="manual",
        )
    await services.commit()
    return dict(pairs)


FIVE = (
    ("one", "первый"),
    ("two", "второй"),
    ("three", "третий"),
    ("four", "четвертый"),
    ("five", "пятый"),
)


def practice_buttons(recording: RecordingSession) -> list[InlineKeyboardButton]:
    return [
        button
        for button in recording.last_buttons()
        if (button.callback_data or "").startswith("pr:answer")
    ]


def correct_data(recording: RecordingSession, translations: dict[str, str]) -> str:
    text = recording.last_text()
    term = next(term for term in translations if f"<b>{term}</b>" in text)
    matches = [
        button.callback_data or ""
        for button in recording.last_buttons()
        if button.text == translations[term]
    ]
    assert matches, (term, [button.text for button in recording.last_buttons()])
    return matches[0]


def wrong_data(recording: RecordingSession, translations: dict[str, str]) -> str:
    text = recording.last_text()
    term = next(term for term in translations if f"<b>{term}</b>" in text)
    matches = [
        button.callback_data or ""
        for button in recording.last_buttons()
        if button.text != translations[term] and (button.callback_data or "").startswith("pr:")
    ]
    assert matches, [button.text for button in recording.last_buttons()]
    return matches[0]


def last_callback_answer(recording: RecordingSession) -> AnswerCallbackQuery:
    answers = [m for m in recording.requests if isinstance(m, AnswerCallbackQuery)]
    assert answers, recording.requests
    return answers[-1]


def edited_text(recording: RecordingSession, needle: str) -> str:
    """The verdict is edited into the question message, not sent anew."""
    matches = [text for text in recording.sent_texts() if needle in text]
    assert matches, recording.sent_texts()
    return matches[-1]


async def play_all_correct(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    translations: dict[str, str],
    *,
    update_id: int = 200,
) -> str:
    for step in range(len(translations) + 1):
        text = recording.last_text()
        if "Тренировка завершена" in text:
            return text
        await tap(
            dispatcher,
            bot,
            correct_data(recording, translations),
            update_id=update_id + step,
        )
    raise AssertionError("the quiz never finished")


async def test_practice_shows_a_four_option_question(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
) -> None:
    await seed_seen(services, user, *FIVE)

    await send(dispatcher, bot, "/practice", update_id=100)

    assert len(practice_buttons(recording)) == 4
    assert "Выберите перевод" in recording.last_text()


async def test_the_menu_button_starts_practice(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
) -> None:
    await seed_seen(services, user, *FIVE)

    await send(dispatcher, bot, texts.BTN_PRACTICE, update_id=100)

    assert len(practice_buttons(recording)) == 4


async def test_a_correct_tap_advances_to_the_next_question(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
) -> None:
    translations = await seed_seen(services, user, *FIVE)
    await send(dispatcher, bot, "/practice", update_id=100)

    await tap(dispatcher, bot, correct_data(recording, translations), update_id=101)

    assert "✅ Верно" in edited_text(recording, "✅ Верно")
    assert len(practice_buttons(recording)) == 4


async def test_a_wrong_tap_shows_the_right_answer(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
) -> None:
    translations = await seed_seen(services, user, *FIVE)
    await send(dispatcher, bot, "/practice", update_id=100)

    await tap(dispatcher, bot, wrong_data(recording, translations), update_id=101)

    assert "❌ Правильно:" in edited_text(recording, "❌ Правильно:")


async def test_finishing_reports_the_score_and_writes_nothing(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    translations = await seed_seen(services, user, *FIVE)
    await send(dispatcher, bot, "/practice", update_id=100)

    final = await play_all_correct(dispatcher, bot, recording, translations)

    assert "Тренировка завершена: <b>5</b> из <b>5</b> верно" in final

    async with session_factory() as check:
        assert await check.scalar(select(func.count(Review.id))) == 0
        cards = (await check.scalars(select(Card))).all()
        assert {card.reps for card in cards} == {1}


async def test_stale_and_repeated_taps_alert(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
) -> None:
    translations = await seed_seen(services, user, *FIVE)
    await send(dispatcher, bot, "/practice", update_id=100)
    stale = correct_data(recording, translations)

    await tap(dispatcher, bot, stale, update_id=101)
    await tap(dispatcher, bot, stale, update_id=102)

    assert last_callback_answer(recording).show_alert


async def test_restarting_invalidates_the_old_buttons(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
) -> None:
    translations = await seed_seen(services, user, *FIVE)
    await send(dispatcher, bot, "/practice", update_id=100)
    stale = correct_data(recording, translations)

    await send(dispatcher, bot, "/practice", update_id=101)
    await tap(dispatcher, bot, stale, update_id=102)

    assert last_callback_answer(recording).show_alert


async def test_an_empty_review_queue_offers_practice(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
) -> None:
    await seed_seen(services, user, *FIVE)

    await send(dispatcher, bot, "/review", update_id=100)

    offer = [
        button for button in recording.last_buttons() if "pr:start" in (button.callback_data or "")
    ]
    assert offer


async def test_too_few_cards_says_so(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
) -> None:
    await seed_seen(services, user, ("one", "первый"), ("two", "второй"))

    await send(dispatcher, bot, "/practice", update_id=100)

    assert "хотя бы 4" in recording.last_text()
