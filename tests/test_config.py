"""Defaults that protect the public bot from accidental load spikes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eng_remember.config import BotSettings, LlmSettings


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
    assert llm.reasoning_effort == "none"


def test_inbound_guards_have_safe_defaults() -> None:
    bot = BotSettings()

    assert bot.messages_per_minute == 10
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
