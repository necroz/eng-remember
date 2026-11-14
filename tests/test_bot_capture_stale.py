"""Buttons from an older message must not act on the flow that is running now.

Every inline keyboard the bot sends can outlive the moment it belonged to: the
chat keeps it, and a tap on it is delivered long after. A stale tap has to be
answered (an unanswered callback spins forever) and has to leave whatever flow
is active untouched.
"""

from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot.callbacks import ClarifyCB, ImportCB
from eng_remember.models.card import Card
from eng_remember.models.review import Review
from eng_remember.utils.time import utcnow
from tests.conftest import RecordingSession, make_due
from tests.test_bot_files import CSV_TEXT, patch_file, send_file
from tests.test_bot_flow import send, tap


def callback_answers(recording: RecordingSession) -> list[AnswerCallbackQuery]:
    return [method for method in recording.requests if isinstance(method, AnswerCallbackQuery)]


def last_callback_answer(recording: RecordingSession) -> AnswerCallbackQuery:
    answers = callback_answers(recording)
    assert answers, recording.requests
    return answers[-1]


async def test_stale_cancel_does_not_kill_review(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An «Отмена» left over from a finished import must not clear a review."""
    await make_due(session_factory)
    await send(dispatcher, bot, "/review", update_id=20)
    assert "ubiquitous" in recording.last_text()
    recording.clear()

    await tap(dispatcher, bot, ImportCB(action="cancel").pack(), update_id=21)

    assert last_callback_answer(recording).show_alert
    # The review is alive: an answer typed now is graded against the shown card.
    recording.clear()
    await send(dispatcher, bot, "вездесущий", update_id=22, date=utcnow())
    async with session_factory() as check:
        assert await check.scalar(select(func.count(Review.id))) == 1


async def test_stale_confirm_ignored(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A «Сохранить» with no pending batch saves nothing and says why."""
    await tap(dispatcher, bot, ImportCB(action="confirm").pack(), update_id=30)

    assert last_callback_answer(recording).show_alert
    async with session_factory() as check:
        assert await check.scalar(select(func.count(Card.id))) == 0


async def test_stale_skip_ignored(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
) -> None:
    """«Реши сам» outside a clarification is stale rather than a command."""
    await tap(dispatcher, bot, ClarifyCB(action="skip").pack(), update_id=31)

    assert last_callback_answer(recording).show_alert


async def test_unknown_callback_answered(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
) -> None:
    """A payload no router knows still gets its spinner dismissed."""
    await tap(dispatcher, bot, "xx:1", update_id=32)

    assert callback_answers(recording)
    assert last_callback_answer(recording).show_alert


async def test_text_during_file_import_does_not_discard_it(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message typed over a pending import is answered, not captured.

    The catch-all used to treat it as a new batch, which silently replaced the
    import waiting for confirmation.
    """
    patch_file(monkeypatch, CSV_TEXT.encode("utf-8"))
    await send_file(dispatcher, bot)
    assert "импорта" in recording.last_text()
    recording.clear()

    await send(dispatcher, bot, "hello — привет", update_id=6)

    assert "подтвердите или отмените импорт" in recording.last_text().lower()
    # The import still owns the flow: its own button saves the file's words.
    await tap(dispatcher, bot, "fi:confirm", update_id=7)
    async with session_factory() as check:
        terms = set((await check.scalars(select(Card.term))).all())
    assert {"run", "swim"} <= terms
