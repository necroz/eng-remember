"""Small utilities shared by the test modules.

Two kinds of things live here: the verbs every handler test uses (send a
message, press a button) and the environment hygiene the settings fixture
needs. Both were copied into several modules before, which meant a fix in one
copy silently left the others behind.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from aiogram import Bot, Dispatcher

from tests.factories import make_callback_update, make_message_update

__all__ = ["neutralize_settings_env", "send", "tap"]

# Environment names ``Settings`` reads: the top-level fields and every nested
# section (pydantic-settings joins nesting with a double underscore). Only
# names starting like these can change what a test measures.
SETTINGS_ENV_PREFIXES = (
    "BOT_TOKEN",
    "BOT__",
    "DATABASE_URL",
    "DB_",
    "LOG_",
    "LLM__",
    "SRS__",
    "REVIEW__",
    "STREAK__",
    "DISCOVERY__",
    "REMINDERS__",
)


def neutralize_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable that could leak a developer's shell into ``Settings``.

    ``monkeypatch`` records each removal, so the developer's environment comes
    back once the test is over. Values such as ``LLM__API_KEY`` exported to try
    the bot locally would otherwise change what the suite proves: a test
    asserting the rule-based fallback would quietly be handed a configured key.
    """
    for name in list(os.environ):
        if name.upper().startswith(SETTINGS_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)


async def send(dispatcher: Dispatcher, bot: Bot, text: str, **kwargs: Any) -> None:
    """Feed an inbound message through the real dispatcher."""
    await dispatcher.feed_update(bot, make_message_update(text, **kwargs))


async def tap(dispatcher: Dispatcher, bot: Bot, data: str, **kwargs: Any) -> None:
    """Feed a button press through the real dispatcher."""
    await dispatcher.feed_update(bot, make_callback_update(data, **kwargs))
