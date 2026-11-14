"""The treatment menu for words that keep failing."""

from __future__ import annotations

import asyncio

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot import texts
from eng_remember.bot.callbacks import LeechCB, ReviewCB
from eng_remember.models.card import Card
from eng_remember.services.llm.schema import MnemonicResponse
from tests.conftest import RecordingSession, make_due
from tests.factories import ScriptedClient
from tests.test_bot_flow import send, tap


async def fail_card(
    dispatcher: Dispatcher,
    bot: Bot,
    card_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    round_no: int,
) -> None:
    """One full «Не знаю» pass over the card."""
    await make_due(session_factory)
    await send(dispatcher, bot, "/review", update_id=100 + round_no * 2)
    await tap(
        dispatcher,
        bot,
        ReviewCB(action="dont_know", card_id=card_id).pack(),
        update_id=101 + round_no * 2,
    )


def leech_messages(recording: RecordingSession) -> list[SendMessage]:
    return [
        method
        for method in recording.requests
        if isinstance(method, SendMessage) and "не даётся" in (method.text or "")
    ]


def leech_menu_buttons(recording: RecordingSession) -> list[str]:
    messages = leech_messages(recording)
    assert messages, recording.sent_texts()
    markup = messages[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [button.callback_data or "" for row in markup.inline_keyboard for button in row]


def last_callback_answer(recording: RecordingSession) -> AnswerCallbackQuery:
    answers = [m for m in recording.requests if isinstance(m, AnswerCallbackQuery)]
    assert answers, recording.requests
    return answers[-1]


def assert_leech_menu_is_back(recording: RecordingSession) -> None:
    """The outcome message still offers the remaining decisions."""
    data = recording.last_button_data()
    assert any(item.startswith("lc:pause") for item in data), data
    assert any(item.startswith("lc:leave") for item in data), data


async def test_the_menu_appears_on_the_fourth_failure(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for round_no in range(4):
        await fail_card(dispatcher, bot, saved_card.id, session_factory, round_no=round_no)

    assert len(leech_messages(recording)) == 1
    assert "4-й провал" in (leech_messages(recording)[-1].text or "")
    buttons = leech_menu_buttons(recording)
    assert any(data.startswith("lc:mnemonic") for data in buttons)
    assert any(data.startswith("lc:example") for data in buttons)
    assert any(data.startswith("lc:pause") for data in buttons)
    assert any(data.startswith("lc:leave") for data in buttons)


async def test_the_menu_is_not_repeated_before_the_next_threshold(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for round_no in range(5):
        await fail_card(dispatcher, bot, saved_card.id, session_factory, round_no=round_no)

    assert len(leech_messages(recording)) == 1


async def test_the_mnemonic_action_saves_it_into_notes(
    llm_dispatcher: Dispatcher,
    llm: ScriptedClient,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm.push_mnemonic({"mnemonic": "«Юбик» — юбка везде."})
    for round_no in range(4):
        await fail_card(llm_dispatcher, bot, saved_card.id, session_factory, round_no=round_no)

    await tap(
        llm_dispatcher,
        bot,
        LeechCB(action="mnemonic", card_id=saved_card.id).pack(),
        update_id=300,
    )

    async with session_factory() as check:
        card = await check.get(Card, saved_card.id)
    assert card is not None
    assert card.notes == "«Юбик» — юбка везде."
    assert llm.calls[-1]["purpose"] == "mnemonic"
    assert "Мнемоника" in recording.last_text()


async def test_the_example_action_adds_a_sentence(
    llm_dispatcher: Dispatcher,
    llm: ScriptedClient,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm.push_examples({"examples": ["It is ubiquitous now."]})
    for round_no in range(4):
        await fail_card(llm_dispatcher, bot, saved_card.id, session_factory, round_no=round_no)

    await tap(
        llm_dispatcher,
        bot,
        LeechCB(action="example", card_id=saved_card.id).pack(),
        update_id=300,
    )

    async with session_factory() as check:
        card = await check.get(Card, saved_card.id)
    assert card is not None
    assert card.examples == ["It is ubiquitous now."]


async def test_the_pause_action_suspends_the_card(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for round_no in range(4):
        await fail_card(dispatcher, bot, saved_card.id, session_factory, round_no=round_no)

    await tap(
        dispatcher,
        bot,
        LeechCB(action="pause", card_id=saved_card.id).pack(),
        update_id=300,
    )

    async with session_factory() as check:
        card = await check.get(Card, saved_card.id)
    assert card is not None
    assert card.is_suspended is True
    assert "/paused" in recording.last_text()


async def test_the_leave_action_closes_the_menu_quietly(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for round_no in range(4):
        await fail_card(dispatcher, bot, saved_card.id, session_factory, round_no=round_no)

    await tap(
        dispatcher,
        bot,
        LeechCB(action="leave", card_id=saved_card.id).pack(),
        update_id=300,
    )

    assert "как есть" in recording.last_text()


async def test_model_actions_alert_when_no_model_is_configured(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for round_no in range(4):
        await fail_card(dispatcher, bot, saved_card.id, session_factory, round_no=round_no)

    await tap(
        dispatcher,
        bot,
        LeechCB(action="mnemonic", card_id=saved_card.id).pack(),
        update_id=300,
    )

    assert last_callback_answer(recording).show_alert


async def test_mnemonic_failure_keeps_the_leech_menu_without_a_model(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An offline model fails one action, not the whole menu.

    The «Готовлю…» edit takes the buttons away before the call, so the failure
    path has to put them back: otherwise «Пауза» and «Оставить как есть» are
    lost for a card the menu was shown for.
    """
    for round_no in range(4):
        await fail_card(dispatcher, bot, saved_card.id, session_factory, round_no=round_no)

    await tap(
        dispatcher,
        bot,
        LeechCB(action="mnemonic", card_id=saved_card.id).pack(),
        update_id=300,
    )

    assert texts.llm_required() in recording.last_text()
    assert_leech_menu_is_back(recording)


async def test_example_failure_keeps_the_leech_menu_without_a_model(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for round_no in range(4):
        await fail_card(dispatcher, bot, saved_card.id, session_factory, round_no=round_no)

    await tap(
        dispatcher,
        bot,
        LeechCB(action="example", card_id=saved_card.id).pack(),
        update_id=300,
    )

    assert texts.llm_required() in recording.last_text()
    assert_leech_menu_is_back(recording)


async def test_an_unexpected_error_still_restores_the_leech_menu(
    llm_dispatcher: Dispatcher,
    llm: ScriptedClient,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A crash in the model call must not leave the message on «Готовлю…»."""
    llm.push_mnemonic(RuntimeError("model exploded"))
    for round_no in range(4):
        await fail_card(llm_dispatcher, bot, saved_card.id, session_factory, round_no=round_no)

    await tap(
        llm_dispatcher,
        bot,
        LeechCB(action="mnemonic", card_id=saved_card.id).pack(),
        update_id=300,
    )

    assert texts.leech_preparing() not in recording.last_text()
    assert texts.llm_failed() in recording.last_text()
    assert_leech_menu_is_back(recording)


async def test_double_mnemonic_tap_single_call(
    llm_dispatcher: Dispatcher,
    llm: ScriptedClient,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two taps on «Мнемоника» while the model answers buy one call, not two.

    The first tap replaces the menu with «Готовлю…» and takes its buttons away,
    but the second tap is already on its way; without a reservation it would
    start a second model call and overwrite the first answer.
    """
    llm.push_mnemonic({"mnemonic": "«Юбик» — юбка везде."})
    for round_no in range(4):
        await fail_card(llm_dispatcher, bot, saved_card.id, session_factory, round_no=round_no)

    entered = asyncio.Event()
    gate = asyncio.Event()
    original = llm.complete_json

    async def gated(
        *,
        purpose: str,
        system: str,
        user: str,
        schema: object,
        user_id: int | None = None,
    ) -> object:
        if schema is MnemonicResponse:
            entered.set()
            await gate.wait()
        return await original(
            purpose=purpose, system=system, user=user, schema=schema, user_id=user_id
        )

    monkeypatch.setattr(llm, "complete_json", gated)
    data = LeechCB(action="mnemonic", card_id=saved_card.id).pack()
    recording.clear()

    first = asyncio.create_task(tap(llm_dispatcher, bot, data, update_id=300))
    await asyncio.wait_for(entered.wait(), timeout=2)
    second = asyncio.create_task(tap(llm_dispatcher, bot, data, update_id=301))

    # The second tap must be answered without waiting for the first call: a
    # blocked tap is the symptom of a second model call being started.
    done, _ = await asyncio.wait({first, second}, timeout=1)
    try:
        assert second in done
        assert last_callback_answer(recording).show_alert
    finally:
        gate.set()
        await asyncio.gather(first, second, return_exceptions=True)

    assert len([call for call in llm.calls if call["purpose"] == "mnemonic"]) == 1
