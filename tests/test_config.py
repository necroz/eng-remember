"""Defaults that protect the public bot from accidental load spikes."""

from __future__ import annotations

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


def test_inbound_guards_have_safe_defaults() -> None:
    bot = BotSettings()

    assert bot.messages_per_minute == 10
    assert bot.callbacks_per_minute == 30
    assert bot.duplicate_message_ttl_seconds == 60.0
    assert bot.admin_chat_id is None
