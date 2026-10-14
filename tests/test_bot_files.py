"""CSV export and import through the chat."""

from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, SendDocument
from aiogram.types import BufferedInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot.callbacks import FileImportCB
from eng_remember.models.card import Card
from eng_remember.models.deck import Deck
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from eng_remember.services.csv_io import MAX_FILE_BYTES
from eng_remember.utils.time import utcnow
from tests.conftest import RecordingSession, make_due
from tests.factories import ScriptedClient, make_document_update
from tests.test_bot_flow import send, tap

CSV_TEXT = "run;бежать;;;;;sport|basic;Бег\nswim;плавать;;\n"


async def seed_due_card(
    services: Services, user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await services.card_service.add_draft(user, CardDraft(term="run", translation="бежать"))
    await services.commit()
    await make_due(session_factory, "run")


def patch_file(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    async def fake_read(bot: Bot, document: object) -> bytes:
        return payload

    monkeypatch.setattr("eng_remember.bot.routers.files._read_document", fake_read)


async def send_file(
    dispatcher: Dispatcher,
    bot: Bot,
    *,
    file_name: str = "words.csv",
    update_id: int = 5,
) -> None:
    await dispatcher.feed_update(bot, make_document_update(file_name, update_id=update_id))


def button_data(recording: RecordingSession, needle: str) -> str:
    matches = [data for data in recording.last_button_data() if needle in data]
    assert matches, f"no button matching {needle!r} among {recording.last_button_data()}"
    return matches[0]


def last_callback_answer(recording: RecordingSession) -> AnswerCallbackQuery:
    answers = [m for m in recording.requests if isinstance(m, AnswerCallbackQuery)]
    assert answers, recording.requests
    return answers[-1]


async def test_export_sends_a_csv_file(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
) -> None:
    deck = await services.decks.create(user.id, "Бег")
    await services.card_service.add_draft(
        user,
        CardDraft(term="run", translation="бежать", tags=("sport",)),
        deck=deck,
    )
    await services.card_service.add_draft(user, CardDraft(term="swim", translation="плавать"))
    await services.commit()

    await send(dispatcher, bot, "/export", update_id=100)

    method = recording.requests[-1]
    assert isinstance(method, SendDocument)
    document = method.document
    assert isinstance(document, BufferedInputFile)
    assert (document.filename or "").endswith(".csv")
    content = document.data.decode("utf-8-sig")
    assert content.splitlines()[0].startswith("term;translation;")
    assert "run;бежать" in content
    assert content.count("run;бежать") == 1  # one row per word, not per card
    assert "Бег" in content


async def test_import_command_explains_the_format(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
) -> None:
    await send(dispatcher, bot, "/import", update_id=100)

    assert "CSV" in recording.last_text()


async def test_preview_then_confirm_saves_cards_and_decks(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    patch_file(monkeypatch, CSV_TEXT.encode("utf-8"))

    await send_file(dispatcher, bot)
    preview = recording.last_text()
    assert "run" in preview
    assert "swim" in preview

    await tap(dispatcher, bot, button_data(recording, "fi:confirm"), update_id=10)

    async with session_factory() as check:
        cards = {card.term: card for card in (await check.scalars(select(Card))).all()}
        assert set(cards) == {"run", "swim"}
        assert len((await check.scalars(select(Card))).all()) == 4  # a pair per word
        assert cards["run"].tags == ["sport", "basic"]
        decks = {deck.name: deck for deck in (await check.scalars(select(Deck))).all()}
        assert "Бег" in decks
        assert cards["run"].deck_id == decks["Бег"].id
        assert cards["swim"].deck_id == decks["Основная колода"].id


async def test_existing_words_are_counted_and_not_duplicated(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    monkeypatch: pytest.MonkeyPatch,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await services.card_service.add_draft(user, CardDraft(term="run", translation="бежать"))
    await services.commit()
    patch_file(monkeypatch, CSV_TEXT.encode("utf-8"))

    await send_file(dispatcher, bot)
    assert "уже было: 1" in recording.last_text()

    await tap(dispatcher, bot, button_data(recording, "fi:confirm"), update_id=10)

    async with session_factory() as check:
        cards = (await check.scalars(select(Card))).all()
        assert sorted(card.term for card in cards) == ["run", "run", "swim", "swim"]


async def test_rows_without_translation_are_skipped_without_a_model(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    patch_file(monkeypatch, b"hike;\nrun;\xd0\xb1\xd0\xb5\xd0\xb6\xd0\xb0\xd1\x82\xd1\x8c\n")

    await send_file(dispatcher, bot)
    preview = recording.last_text()
    assert "без перевода пропущено: 1" in preview

    await tap(dispatcher, bot, button_data(recording, "fi:confirm"), update_id=10)

    async with session_factory() as check:
        cards = (await check.scalars(select(Card))).all()
        assert [card.term for card in cards] == ["run", "run"]


async def test_the_model_translates_missing_meanings(
    llm_dispatcher: Dispatcher,
    llm: ScriptedClient,
    bot: Bot,
    recording: RecordingSession,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm.push_translations({"translations": [{"term": "hike", "translation": "ходить в походы"}]})
    patch_file(monkeypatch, b"hike;\n")

    await send_file(llm_dispatcher, bot)
    assert "ходить в походы" in recording.last_text()

    await tap(llm_dispatcher, bot, button_data(recording, "fi:confirm"), update_id=10)

    async with session_factory() as check:
        cards = (await check.scalars(select(Card))).all()
        assert {card.term for card in cards} == {"hike"}
        assert {card.translation for card in cards} == {"ходить в походы"}


async def test_cancelling_imports_nothing(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    patch_file(monkeypatch, CSV_TEXT.encode("utf-8"))
    await send_file(dispatcher, bot)

    await tap(dispatcher, bot, button_data(recording, "fi:cancel"), update_id=10)

    assert "отмен" in recording.last_text()
    async with session_factory() as check:
        assert (await check.scalars(select(Card))).all() == []


async def test_a_repeated_confirm_alerts(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_file(monkeypatch, CSV_TEXT.encode("utf-8"))
    await send_file(dispatcher, bot)
    confirm = button_data(recording, "fi:confirm")

    await tap(dispatcher, bot, confirm, update_id=10)
    await tap(dispatcher, bot, confirm, update_id=11)

    assert last_callback_answer(recording).show_alert


async def test_a_too_big_file_is_refused(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    patch_file(monkeypatch, b"x" * (MAX_FILE_BYTES + 1))

    await send_file(dispatcher, bot)

    assert "большой" in recording.last_text()
    async with session_factory() as check:
        assert (await check.scalars(select(Card))).all() == []


async def test_an_empty_file_says_so(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_file(monkeypatch, b"")

    await send_file(dispatcher, bot)

    assert "не нашлось" in recording.last_text()


async def test_cp1251_files_are_read(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    patch_file(monkeypatch, "run;бежать\n".encode("cp1251"))

    await send_file(dispatcher, bot)
    await tap(dispatcher, bot, button_data(recording, "fi:confirm"), update_id=10)

    async with session_factory() as check:
        cards = (await check.scalars(select(Card))).all()
        assert {card.translation for card in cards} == {"бежать"}


async def test_other_documents_get_the_format_hint(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
) -> None:
    await send_file(dispatcher, bot, file_name="notes.pdf")

    assert "CSV" in recording.last_text()


async def test_a_stale_cancel_does_not_break_another_flow(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_due_card(services, user, session_factory)
    await send(dispatcher, bot, "/review", update_id=100)
    assert "run" in recording.last_text()

    await tap(dispatcher, bot, FileImportCB(action="cancel").pack(), update_id=101)

    assert last_callback_answer(recording).show_alert

    # The review must still be alive: the answer is graded, not swallowed by a
    # capture parser that took over the cleared state.
    await send(dispatcher, bot, "бежать", update_id=102, date=utcnow())
    assert any("Дальше через" in text for text in recording.sent_texts())


async def test_a_giant_csv_field_is_refused(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_file(monkeypatch, b'run;"' + b"x" * 131_073 + b'"\n')

    await send_file(dispatcher, bot)

    assert "прочитать" in recording.last_text()


async def test_a_utf16_file_is_refused(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_file(monkeypatch, "run;бежать\n".encode("utf-16"))

    await send_file(dispatcher, bot)

    assert "прочитать" in recording.last_text()


def test_confirm_callback_shape() -> None:
    assert FileImportCB(action="confirm").pack() == "fi:confirm"
