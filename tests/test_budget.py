"""Alerts for the bot-wide token budget."""

from __future__ import annotations

import pytest

from eng_remember.config import LlmSettings
from eng_remember.services.llm.budget import BudgetAlert, GlobalBudgetMonitor


def settings(*, limit: int = 1000, percent: int = 80) -> LlmSettings:
    return LlmSettings(max_tokens_per_day_global=limit, global_alert_percent=percent)


async def test_budget_warns_once_at_eighty_percent_and_once_when_spent() -> None:
    monitor = GlobalBudgetMonitor()
    alerts: list[BudgetAlert] = []

    async def notify(alert: BudgetAlert) -> None:
        alerts.append(alert)

    monitor.set_notifier(notify)
    config = settings()

    await monitor.check(799, config)
    assert alerts == []

    await monitor.check(800, config)
    assert [(a.level, a.used, a.limit) for a in alerts] == [("warning", 800, 1000)]

    await monitor.check(900, config)
    assert len(alerts) == 1

    await monitor.check(1000, config)
    assert [(a.level, a.used, a.limit) for a in alerts[-1:]] == [("critical", 1000, 1000)]

    await monitor.check(1100, config)
    assert len(alerts) == 2


async def test_no_notifier_is_not_an_error() -> None:
    monitor = GlobalBudgetMonitor()

    await monitor.check(1000, settings())


async def test_a_broken_notifier_does_not_break_the_call() -> None:
    monitor = GlobalBudgetMonitor()

    async def broken(alert: BudgetAlert) -> None:
        raise RuntimeError("telegram down")

    monitor.set_notifier(broken)

    await monitor.check(1000, settings())


async def test_zero_global_budget_disables_the_monitor() -> None:
    monitor = GlobalBudgetMonitor()
    alerts: list[BudgetAlert] = []

    async def notify(alert: BudgetAlert) -> None:
        alerts.append(alert)

    monitor.set_notifier(notify)

    await monitor.check(1_000_000, settings(limit=0))

    assert alerts == []


@pytest.mark.parametrize("percent", [1, 80, 100])
def test_the_alert_threshold_is_configurable(percent: int) -> None:
    assert settings(percent=percent).global_alert_percent == percent
