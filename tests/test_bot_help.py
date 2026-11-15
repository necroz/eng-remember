"""One help screen, whatever way the learner asks for it.

``/help`` and the «Помощь» button promised the same thing and used to show two
different texts; a command missing from one of them is a command the user never
learns about.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher

from eng_remember.bot import texts
from tests.conftest import RecordingSession
from tests.helpers import send

HELP_COMMANDS = (
    "/start",
    "/help",
    "/review",
    "/practice",
    "/add",
    "/stats",
    "/decks",
    "/paused",
    "/import",
    "/export",
    "/level",
    "/remind",
    "/tz",
    "/limit",
    "/settings",
)


async def test_help_and_settings_help_identical(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
) -> None:
    await send(dispatcher, bot, "/help", update_id=10)
    command_text = recording.last_text()

    await send(dispatcher, bot, texts.BTN_HELP, update_id=11)
    button_text = recording.last_text()

    assert command_text == button_text
    for command in HELP_COMMANDS:
        assert command in command_text


async def test_settings_keeps_the_preferences_screen(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
) -> None:
    """«Помощь» may share the help text, but /settings is still settings."""
    await send(dispatcher, bot, "/settings", update_id=12)

    sent = recording.last_text()
    assert "Настройки" in sent
    assert "Часовой пояс" in sent
