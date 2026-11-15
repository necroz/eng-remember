"""Deck management: the /decks list, renaming and deleting a deck."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot import texts
from eng_remember.bot.callbacks import DeckCB
from eng_remember.models.card import Card
from eng_remember.models.deck import Deck
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from eng_remember.utils.time import utcnow
from tests.conftest import RecordingSession, make_due
from tests.helpers import send, tap


def last_callback_answer(recording: RecordingSession) -> AnswerCallbackQuery:
    answers = [m for m in recording.requests if isinstance(m, AnswerCallbackQuery)]
    assert answers, recording.requests
    return answers[-1]


async def default_deck(services: Services, user: User) -> Deck:
    deck = await services.card_service.default_deck(user)
    # Release the read transaction: the dispatcher writes through a session of
    # its own, and a fixture session holding one would look like a lock.
    await services.commit()
    return deck


async def make_deck(services: Services, user: User, name: str) -> Deck:
    deck = await services.decks.create(user.id, name)
    await services.commit()
    return deck


async def deck_name(session_factory: async_sessionmaker[AsyncSession], deck_id: int) -> str | None:
    """The stored name, read back through a session of its own."""
    async with session_factory() as check:
        deck = await check.get(Deck, deck_id)
        return None if deck is None else deck.name


async def test_decks_list_offers_rename_and_delete_buttons(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
) -> None:
    deck = await make_deck(services, user, "Книги")

    await send(dispatcher, bot, "/decks")

    data = recording.last_button_data()
    assert DeckCB(action="rn", deck_id=deck.id).pack() in data
    assert DeckCB(action="dl", deck_id=deck.id).pack() in data
    assert "Книги" in recording.last_text()


async def test_default_deck_cannot_be_deleted(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    deck = await default_deck(services, user)

    await tap(dispatcher, bot, DeckCB(action="dl", deck_id=deck.id).pack(), update_id=10)
    refused = last_callback_answer(recording)
    assert refused.show_alert
    assert "нельзя" in (refused.text or "")

    # Confirming a deletion that was never offered must fail the same way.
    await tap(dispatcher, bot, DeckCB(action="ok", deck_id=deck.id).pack(), update_id=11)
    assert last_callback_answer(recording).show_alert

    async with session_factory() as check:
        assert await check.get(Deck, deck.id) is not None


async def test_delete_deck_removes_its_cards(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    deck = await make_deck(services, user, "Книги")
    card = await services.card_service.add_draft(
        user, CardDraft(term="tome", translation="фолиант"), deck=deck
    )
    await services.commit()

    await tap(dispatcher, bot, DeckCB(action="dl", deck_id=deck.id).pack(), update_id=10)
    assert "Удалить колоду" in recording.last_text()
    await tap(dispatcher, bot, DeckCB(action="ok", deck_id=deck.id).pack(), update_id=11)

    async with session_factory() as check:
        assert await check.get(Deck, deck.id) is None
        assert await check.get(Card, card.id) is None
        assert await check.scalar(select(func.count(Card.id)).where(Card.deck_id == deck.id)) == 0

    # The confirmation message turns into the refreshed list.
    assert "Книги" not in recording.last_text()


async def test_cancelled_deletion_takes_the_confirm_buttons_away(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Saying «no» must not leave a live «Удалить» under the message.

    aiogram drops a falsy ``reply_markup`` from an edit request, so an edit
    without one does not clear the keyboard — Telegram keeps it. That would
    leave the confirmation one tap away after the learner cancelled, and the
    confirm handler does not ask again: the deck and its cards would be gone.
    The cancellation therefore has to carry an explicit empty markup.
    """
    deck = await make_deck(services, user, "Книги")
    card = await services.card_service.add_draft(
        user, CardDraft(term="tome", translation="фолиант"), deck=deck
    )
    await services.commit()

    await tap(dispatcher, bot, DeckCB(action="dl", deck_id=deck.id).pack(), update_id=10)
    confirm = DeckCB(action="ok", deck_id=deck.id).pack()
    assert confirm in recording.last_button_data()

    await tap(dispatcher, bot, DeckCB(action="dc", deck_id=deck.id).pack(), update_id=11)

    markup = recording.outbound[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup), recording.requests
    assert markup.inline_keyboard == []
    assert confirm not in recording.last_button_data()

    # A second tap on the cancel button is still only a cancellation.
    await tap(dispatcher, bot, DeckCB(action="dc", deck_id=deck.id).pack(), update_id=12)

    async with session_factory() as check:
        assert await check.get(Deck, deck.id) is not None
        assert await check.get(Card, card.id) is not None


async def test_rename_deck_validates_uniqueness(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await make_deck(services, user, "Книги")
    second = await make_deck(services, user, "Фильмы")

    await tap(dispatcher, bot, DeckCB(action="rn", deck_id=second.id).pack(), update_id=10)
    assert "Новое имя" in recording.last_text()

    await send(dispatcher, bot, first.name, update_id=11)

    assert "уже есть" in recording.last_text()
    assert await deck_name(session_factory, second.id) == "Фильмы"


async def test_rename_deck_saves_a_new_name(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    deck = await make_deck(services, user, "Книги")

    await tap(dispatcher, bot, DeckCB(action="rn", deck_id=deck.id).pack(), update_id=10)
    await send(dispatcher, bot, "  Чтиво  ", update_id=11)

    async with session_factory() as check:
        renamed = await check.get(Deck, deck.id)
    assert renamed is not None
    assert renamed.name == "Чтиво"
    assert "Чтиво" in recording.last_text()


async def test_a_too_long_name_is_rejected(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    deck = await make_deck(services, user, "Книги")

    await tap(dispatcher, bot, DeckCB(action="rn", deck_id=deck.id).pack(), update_id=10)
    await send(dispatcher, bot, "я" * 65, update_id=11)

    assert "64" in recording.last_text()
    assert await deck_name(session_factory, deck.id) == "Книги"


async def test_stale_deck_button_alert(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
) -> None:
    await tap(dispatcher, bot, DeckCB(action="dl", deck_id=999_999).pack(), update_id=10)

    answer = last_callback_answer(recording)
    assert answer.show_alert
    assert "Колода" in (answer.text or "")


async def test_stale_rename_button_does_not_start_a_flow(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
) -> None:
    before = len(recording.requests)

    await tap(dispatcher, bot, DeckCB(action="rn", deck_id=999_999).pack(), update_id=10)

    assert last_callback_answer(recording).show_alert
    assert not [text for text in recording.sent_texts()[before:] if "Новое имя" in text]


async def test_rename_can_be_cancelled(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    deck = await make_deck(services, user, "Книги")

    await tap(dispatcher, bot, DeckCB(action="rn", deck_id=deck.id).pack(), update_id=10)
    await tap(dispatcher, bot, DeckCB(action="rc", deck_id=deck.id).pack(), update_id=11)
    await send(dispatcher, bot, "Чтиво", update_id=12)

    async with session_factory() as check:
        kept = await check.get(Deck, deck.id)
    assert kept is not None
    assert kept.name == "Книги"


async def test_rename_while_review_is_waiting_keeps_the_run(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Opening the rename dialogue mid-review must not eat the next answer.

    The rename prompt is entered from the deck list while a card is waiting.
    The flow the chat was in is saved and put back, so the name cannot be
    mistaken for a translation and the review still grades what comes after.
    """
    deck = await make_deck(services, user, "Книги")
    await services.card_service.add_draft(user, CardDraft(term="alpha", translation="альфа"))
    await services.card_service.add_draft(user, CardDraft(term="beta", translation="бета"))
    await services.commit()
    await make_due(session_factory)

    await send(dispatcher, bot, "/review", update_id=20)
    await send(dispatcher, bot, "альфа", update_id=21, date=utcnow())
    assert "beta" in recording.last_text()

    await send(dispatcher, bot, texts.BTN_DECKS, update_id=22)
    await tap(dispatcher, bot, DeckCB(action="rn", deck_id=deck.id).pack(), update_id=23)
    await send(dispatcher, bot, "Чтиво", update_id=24)

    assert await deck_name(session_factory, deck.id) == "Чтиво"

    await send(dispatcher, bot, "бета", update_id=25, date=utcnow())
    assert "Повторение завершено" in recording.last_text()
