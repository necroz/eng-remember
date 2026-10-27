"""Defaults that protect the public bot from accidental load spikes."""

from __future__ import annotations

from eng_remember.config import BotSettings, LlmSettings


def test_twenty_updates_may_be_in_flight_per_process() -> None:
    assert BotSettings().tasks_concurrency_limit == 20


def test_free_tier_llm_limits_are_daily() -> None:
    llm = LlmSettings()

    assert llm.max_calls_per_day == 60
    assert llm.max_tokens_per_day == 100_000
