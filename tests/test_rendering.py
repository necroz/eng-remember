"""Outgoing messages never exceed what Telegram accepts."""

from __future__ import annotations

from datetime import date

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery

from eng_remember.bot import rendering, texts
from eng_remember.bot.notifier import BotNotifier
from eng_remember.domain.reminders import ReminderKind
from eng_remember.models.user import User
from eng_remember.services.reminders import ReminderPlan
from tests.conftest import RecordingSession
from tests.factories import telegram_user

SUFFIX = "… (сообщение сокращено)"


def long_wordy_text(lines: int = 400) -> str:
    """A message far beyond the limit, made of complete tagged lines."""
    return "\n".join(
        f"• <b>слово{index}</b> — <i>перевод{index}</i> <code>code{index}</code>"
        for index in range(lines)
    )


def test_fit_text_keeps_short_text_unchanged() -> None:
    text = "<b>run</b> — бежать"

    assert rendering.fit_text(text) == text


def test_fit_text_truncates_on_line_boundary() -> None:
    text = long_wordy_text()

    fitted = rendering.fit_text(text)

    assert len(fitted) <= rendering.TELEGRAM_TEXT_LIMIT
    assert fitted.endswith(SUFFIX)
    body = fitted.removesuffix("\n" + SUFFIX)
    # The cut falls between lines, so the last visible line is complete.
    assert body.endswith("</code>")
    assert body.count("<b>") == body.count("</b>")
    assert body.count("<i>") == body.count("</i>")
    assert body.count("<code>") == body.count("</code>")


def test_fit_text_does_not_cut_inside_a_tag() -> None:
    # The last newline before the limit sits inside a tag, so the cut has to
    # move back to the line boundary before it.
    budget = rendering.TELEGRAM_TEXT_LIMIT - len("\n" + SUFFIX)
    head = "a" * (budget - 20)
    text = head + '\n<b class="x\ny">хвост</b>\n' + "z" * 500

    fitted = rendering.fit_text(text)

    assert len(fitted) <= rendering.TELEGRAM_TEXT_LIMIT
    assert "<b" not in fitted
    assert fitted == head + "\n" + SUFFIX


def test_fit_text_closes_tags_left_open_by_the_cut() -> None:
    text = "<b>" + "x" * 4000 + "\n" + "y" * 500

    fitted = rendering.fit_text(text)

    assert len(fitted) <= rendering.TELEGRAM_TEXT_LIMIT
    body = fitted.removesuffix("\n" + SUFFIX)
    assert body.endswith("</b>")
    assert body.count("<b>") == body.count("</b>")


async def test_edit_or_send_fits_long_text(
    bot: Bot,
    recording: RecordingSession,
) -> None:
    # No message to edit, so the helper falls back to sending a new one.
    query = CallbackQuery(
        id="cb-1",
        from_user=telegram_user(1),
        chat_instance="chat-instance",
        data="x",
        message=None,
    )

    await rendering.edit_or_send(query, long_wordy_text(), bot=bot)

    sent = recording.last_text()
    assert len(sent) <= rendering.TELEGRAM_TEXT_LIMIT
    assert sent.endswith(SUFFIX)


async def test_edit_message_or_send_fits_long_text(
    bot: Bot,
    recording: RecordingSession,
) -> None:
    await rendering.edit_message_or_send(
        bot,
        chat_id=1,
        message_id=None,
        text=long_wordy_text(),
    )

    sent = recording.last_text()
    assert len(sent) <= rendering.TELEGRAM_TEXT_LIMIT
    assert sent.endswith(SUFFIX)


async def test_notifier_fits_long_text(
    bot: Bot,
    recording: RecordingSession,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_body = long_wordy_text()
    monkeypatch.setattr(texts, "queue_reminder", lambda **_: long_body)
    plan = ReminderPlan(
        user=user,
        kind=ReminderKind.MORNING,
        local_day=date(2026, 5, 14),
        due_now=1,
        new_available=0,
        streak_days=1,
    )

    await BotNotifier(bot).send_reminder(plan)

    sent = recording.last_text()
    assert len(sent) <= rendering.TELEGRAM_TEXT_LIMIT
    assert sent.endswith(SUFFIX)


def test_fit_text_measures_utf16_units_with_emoji() -> None:
    """Telegram counts UTF-16 units, and a flag emoji takes four of them.

    The same text is 4096 characters by ``len`` but 8192 units on the wire, so a
    code-point budget lets an over-long message through.
    """
    text = "\n".join(["🇬🇧🇫🇷🇩🇪🇪🇸" * 25] * 30)

    fitted = rendering.fit_text(text)

    assert rendering.text_units(fitted) <= rendering.TELEGRAM_TEXT_LIMIT
    assert fitted.endswith(SUFFIX)
