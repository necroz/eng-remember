"""Defaults that protect the public bot from accidental load spikes."""

from __future__ import annotations

import os

import pytest
from pydantic import SecretStr, ValidationError

from eng_remember.config import (
    BotSettings,
    LlmSettings,
    ReviewSettings,
    Settings,
    require_bot_token,
)
from eng_remember.errors import ConfigurationError
from tests.helpers import neutralize_settings_env


def test_twenty_updates_may_be_in_flight_per_process() -> None:
    assert BotSettings().tasks_concurrency_limit == 20


def test_free_tier_llm_limits_are_daily() -> None:
    llm = LlmSettings()

    assert llm.max_calls_per_day == 60
    assert llm.max_example_calls_per_day == 30
    assert llm.max_tokens_per_day == 100_000
    assert llm.max_tokens_per_day_global == 2_000_000
    assert llm.global_alert_percent == 80


def test_llm_budget_fits_reasoning_and_a_full_discovery_batch() -> None:
    """Reasoning tokens count against the output limit.

    A 20-word request is oversampled to 28 cards; the model used in production
    spent about 4.5k completion tokens on that (reasoning plus JSON), so 1500
    cut it off mid-answer and every retry failed the same way.
    """
    llm = LlmSettings()

    assert llm.max_output_tokens >= 8000
    assert llm.timeout_seconds >= 90
    # Off by default: an unset parameter is not sent at all, so providers that
    # do not know it never reject a request, and a reasoning model keeps its
    # own default unless the operator asks otherwise.
    assert llm.reasoning_effort is None


def test_inbound_guards_have_safe_defaults() -> None:
    bot = BotSettings()

    assert bot.messages_per_minute == 30
    assert bot.callbacks_per_minute == 30
    assert bot.duplicate_message_ttl_seconds == 60.0
    assert bot.admin_chat_id is None


def test_a_review_run_is_a_short_random_portion() -> None:
    bot = BotSettings()

    assert bot.review_session_min_cards == 15
    assert bot.review_session_max_cards == 20


def test_the_session_range_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        BotSettings(review_session_min_cards=20, review_session_max_cards=15)


def test_the_easy_threshold_leaves_room_for_typing() -> None:
    """The grade must reward recall, not typing speed.

    Production answers showed a 5-second bar caught only 1.6% of correct
    answers: the timer runs from the card appearing to the message being sent,
    so it includes reading the front and typing the whole word on a phone.
    The median correct answer took ~14s and the fastest quartile ~10s, so the
    bar sits at the fast-quartile mark instead of below the whole distribution.
    """
    assert ReviewSettings().easy_threshold_seconds == 10.0


def test_parse_mode_markdown_rejected() -> None:
    """The bot's texts are HTML; another parse mode would show raw tags."""
    with pytest.raises(ValidationError):
        BotSettings(parse_mode="MarkdownV2")


def test_parse_mode_html_is_the_only_accepted_value() -> None:
    assert BotSettings().parse_mode == "HTML"


class TestBotToken:
    """The one setting without a default: both entry points refuse to start without it."""

    def test_a_missing_token_explains_where_to_get_one(self) -> None:
        settings = Settings(_env_file=None)

        with pytest.raises(ConfigurationError) as excinfo:
            require_bot_token(settings)

        message = str(excinfo.value)
        assert "BOT_TOKEN" in message
        assert "@BotFather" in message

    @pytest.mark.parametrize("token", ["", "   ", "\n\t "])
    def test_an_empty_or_blank_token_is_rejected(self, token: str) -> None:
        settings = Settings(_env_file=None, bot_token=SecretStr(token))

        with pytest.raises(ConfigurationError, match="BOT_TOKEN"):
            require_bot_token(settings)

    def test_a_configured_token_is_returned_trimmed(self) -> None:
        """Whitespace around the value is a copy-paste artefact, not part of the token."""
        settings = Settings(_env_file=None, bot_token=SecretStr(" 42:TEST-TOKEN\n"))

        assert require_bot_token(settings) == "42:TEST-TOKEN"


class TestEnvironmentIsolation:
    """Exported variables must not change what the suite measures."""

    def test_the_settings_fixture_ignores_an_exported_api_key(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fixture removed it before this test's settings were built."""
        assert settings.llm.api_key is None

    def test_neutralisation_removes_settings_variables_and_keeps_the_rest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM__API_KEY", "sk-test")
        monkeypatch.setenv("BOT_TOKEN", "42:TEST-TOKEN")
        monkeypatch.setenv("REMINDERS__BATCH_SIZE", "5")
        monkeypatch.setenv("UNRELATED_SETTING", "keep")

        neutralize_settings_env(monkeypatch)

        assert "LLM__API_KEY" not in os.environ
        assert "BOT_TOKEN" not in os.environ
        assert "REMINDERS__BATCH_SIZE" not in os.environ
        assert os.environ["UNRELATED_SETTING"] == "keep"
