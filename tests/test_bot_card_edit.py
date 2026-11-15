"""Editing a card's text from the graded result: translation, example, notes."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot.callbacks import CardEditCB, CardEditStepCB
from eng_remember.models.card import Card
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.repositories.users import TelegramProfile, UserRepository
from eng_remember.services.container import Services
from eng_remember.utils.time import utcnow
from tests.conftest import RecordingSession, make_due
from tests.helpers import send, tap


def last_callback_answer(recording: RecordingSession) -> AnswerCallbackQuery:
    answers = [m for m in recording.requests if isinstance(m, AnswerCallbackQuery)]
    assert answers, recording.requests
    return answers[-1]


def edit_button(recording: RecordingSession) -> str:
    """The «Правка» payload on the message that carries the graded result."""
    for method in reversed(recording.outbound):
        if "Дальше через" not in (method.text or ""):
            continue
        markup = method.reply_markup
        if not isinstance(markup, InlineKeyboardMarkup):
            continue
        for row in markup.inline_keyboard:
            for button in row:
                data = button.callback_data or ""
                if data.startswith("ce:"):
                    return data
    raise AssertionError(recording.sent_texts())


async def load_card(session_factory: async_sessionmaker[AsyncSession], card_id: int) -> Card:
    async with session_factory() as check:
        card = await check.get(Card, card_id)
    assert card is not None
    return card


async def graded_card(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    term: str = "ubiquitous",
    translation: str = "вездесущий",
    example: str | None = None,
    notes: str | None = None,
) -> int:
    """A card answered once, so its result message is in the chat history."""
    card = await services.card_service.add_draft(
        user,
        CardDraft(term=term, translation=translation, example=example, notes=notes),
    )
    await services.commit()
    await make_due(session_factory, term)

    await send(dispatcher, bot, "/review", update_id=20)
    await send(dispatcher, bot, translation, update_id=21, date=utcnow())
    return card.id


async def test_the_result_offers_an_edit_button(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    card_id = await graded_card(dispatcher, bot, recording, services, user, session_factory)

    assert edit_button(recording) == CardEditCB(card_id=card_id).pack()


async def test_edit_translation_persists(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    card_id = await graded_card(dispatcher, bot, recording, services, user, session_factory)
    before = await load_card(session_factory, card_id)

    await tap(dispatcher, bot, CardEditCB(card_id=card_id).pack(), update_id=30)
    assert "Перевод сейчас" in recording.last_text()
    await send(dispatcher, bot, "всюдусущий", update_id=31)
    # An empty message is a skipped step: the example stays as it was.
    await send(dispatcher, bot, "   ", update_id=32)
    await tap(
        dispatcher, bot, CardEditStepCB(action="finish", card_id=card_id).pack(), update_id=33
    )

    after = await load_card(session_factory, card_id)
    assert after.translation == "всюдусущий"
    assert "Карточка обновлена" in recording.last_text()
    # Correcting the wording is not an answer: the schedule must not move.
    assert after.reps == before.reps
    assert after.due_at == before.due_at
    assert after.state == before.state


async def test_edit_skip_keeps_old_values(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    card_id = await graded_card(
        dispatcher,
        bot,
        recording,
        services,
        user,
        session_factory,
        example="A ubiquitous truth.",
        notes="из математики",
    )
    before = await load_card(session_factory, card_id)

    await tap(dispatcher, bot, CardEditCB(card_id=card_id).pack(), update_id=30)
    for update_id in (31, 32, 33):
        await tap(
            dispatcher,
            bot,
            CardEditStepCB(action="skip", card_id=card_id).pack(),
            update_id=update_id,
        )

    after = await load_card(session_factory, card_id)
    assert after.translation == before.translation
    assert after.example == before.example
    assert after.notes == before.notes
    assert "Карточка обновлена" in recording.last_text()


async def test_edit_foreign_card_rejected(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    other, _ = await UserRepository(services.session).get_or_create(
        TelegramProfile(telegram_id=999_111, first_name="Other")
    )
    stranger = await services.card_service.add_draft(
        other, CardDraft(term="secret", translation="секрет")
    )
    await services.commit()

    await tap(dispatcher, bot, CardEditCB(card_id=stranger.id).pack(), update_id=30)

    answer = last_callback_answer(recording)
    assert answer.show_alert
    assert "не доступна" in (answer.text or "")
    assert (await load_card(session_factory, stranger.id)).translation == "секрет"


async def test_stale_edit_button_alerts(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
) -> None:
    await tap(dispatcher, bot, CardEditCB(card_id=999_999).pack(), update_id=30)

    answer = last_callback_answer(recording)
    assert answer.show_alert
    assert "не доступна" in (answer.text or "")


async def test_edit_can_be_cancelled(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    card_id = await graded_card(dispatcher, bot, recording, services, user, session_factory)

    await tap(dispatcher, bot, CardEditCB(card_id=card_id).pack(), update_id=30)
    await send(dispatcher, bot, "не тот перевод", update_id=31)
    await tap(
        dispatcher, bot, CardEditStepCB(action="cancel", card_id=card_id).pack(), update_id=32
    )

    assert "Правка отменена" in recording.last_text()
    assert (await load_card(session_factory, card_id)).translation == "вездесущий"


async def test_editing_does_not_break_the_review_run(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The second card is already waiting on screen while the first is edited."""
    first = await services.card_service.add_draft(
        user, CardDraft(term="alpha", translation="альфа")
    )
    await services.card_service.add_draft(user, CardDraft(term="beta", translation="бета"))
    await services.commit()
    await make_due(session_factory)

    await send(dispatcher, bot, "/review", update_id=20)
    await send(dispatcher, bot, "альфа", update_id=21, date=utcnow())
    assert "beta" in recording.last_text()

    await tap(dispatcher, bot, CardEditCB(card_id=first.id).pack(), update_id=30)
    await tap(
        dispatcher, bot, CardEditStepCB(action="finish", card_id=first.id).pack(), update_id=31
    )

    await send(dispatcher, bot, "бета", update_id=32, date=utcnow())

    # The answer to the card that was waiting during the edit was still graded:
    # two cards, two answers, and the run closes.
    assert "Повторение завершено" in recording.last_text()


async def test_update_content_normalises_what_it_stores(
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The service trims before persisting, like every other write path."""
    card = await services.card_service.add_draft(
        user, CardDraft(term="ubiquitous", translation="вездесущий")
    )
    await services.commit()

    await services.card_service.update_content(
        user, card.id, translation="  вездесущий  ", notes="  заметка  "
    )
    await services.commit()

    stored = await load_card(session_factory, card.id)
    assert stored.translation == "вездесущий"
    assert stored.notes == "заметка"


async def test_a_second_edit_tap_keeps_the_review_run(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tapping «Правка» twice must not make the edit its own previous flow.

    The second tap is stale: re-entering would record ``CardEdit.translation``
    as the flow to return to, and finishing would leave a zombie edit state that
    swallows the answer to the card already waiting on screen.
    """
    first = await services.card_service.add_draft(
        user, CardDraft(term="alpha", translation="альфа")
    )
    await services.card_service.add_draft(user, CardDraft(term="beta", translation="бета"))
    await services.commit()
    await make_due(session_factory)

    await send(dispatcher, bot, "/review", update_id=20)
    await send(dispatcher, bot, "альфа", update_id=21, date=utcnow())
    assert "beta" in recording.last_text()

    await tap(dispatcher, bot, CardEditCB(card_id=first.id).pack(), update_id=30)
    await tap(dispatcher, bot, CardEditCB(card_id=first.id).pack(), update_id=31)
    assert last_callback_answer(recording).show_alert

    await tap(
        dispatcher, bot, CardEditStepCB(action="finish", card_id=first.id).pack(), update_id=32
    )
    await send(dispatcher, bot, "бета", update_id=33, date=utcnow())

    assert "Повторение завершено" in recording.last_text()
