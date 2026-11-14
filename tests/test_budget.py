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


async def test_warning_repeats_after_usage_drops() -> None:
    """One alert per episode of exceeding the threshold, not one per process."""
    monitor = GlobalBudgetMonitor()
    alerts: list[BudgetAlert] = []

    async def notify(alert: BudgetAlert) -> None:
        alerts.append(alert)

    monitor.set_notifier(notify)
    config = settings(limit=1000, percent=80)

    await monitor.check(850, config)
    assert [a.level for a in alerts] == ["warning"]

    await monitor.check(500, config)
    assert [a.level for a in alerts] == ["warning"]

    await monitor.check(850, config)
    assert [a.level for a in alerts] == ["warning", "warning"]


async def test_critical_repeats_after_usage_drops() -> None:
    monitor = GlobalBudgetMonitor()
    alerts: list[BudgetAlert] = []

    async def notify(alert: BudgetAlert) -> None:
        alerts.append(alert)

    monitor.set_notifier(notify)
    config = settings(limit=1000, percent=80)

    await monitor.check(1000, config)
    assert [a.level for a in alerts] == ["critical"]

    await monitor.check(1001, config)
    assert [a.level for a in alerts] == ["critical"]

    await monitor.check(600, config)
    assert [a.level for a in alerts] == ["critical"]

    await monitor.check(1000, config)
    assert [a.level for a in alerts] == ["critical", "critical"]


async def test_a_critical_alert_covers_the_warning_band_until_usage_drops() -> None:
    """A dip from critical into warning is the same episode, not a new one."""
    monitor = GlobalBudgetMonitor()
    alerts: list[BudgetAlert] = []

    async def notify(alert: BudgetAlert) -> None:
        alerts.append(alert)

    monitor.set_notifier(notify)
    config = settings(limit=1000, percent=80)

    await monitor.check(1000, config)
    assert [a.level for a in alerts] == ["critical"]

    # Falling below the limit ends the critical episode but stays above the
    # warning threshold: the warning for this spending has already been told.
    await monitor.check(900, config)
    assert [a.level for a in alerts] == ["critical"]

    # Only a real recovery below the threshold starts a new warning episode.
    await monitor.check(700, config)
    await monitor.check(850, config)
    assert [a.level for a in alerts] == ["critical", "warning"]


async def test_reset_forgets_the_warning_flag() -> None:
    monitor = GlobalBudgetMonitor()
    alerts: list[BudgetAlert] = []

    async def notify(alert: BudgetAlert) -> None:
        alerts.append(alert)

    monitor.set_notifier(notify)
    config = settings(limit=1000, percent=80)

    await monitor.check(850, config)
    monitor.reset()
    await monitor.check(850, config)

    assert [a.level for a in alerts] == ["warning", "warning"]


async def test_reset_keeps_notifier() -> None:
    """Resetting the alert state must not silence the admin channel."""
    monitor = GlobalBudgetMonitor()
    alerts: list[BudgetAlert] = []

    async def notify(alert: BudgetAlert) -> None:
        alerts.append(alert)

    monitor.set_notifier(notify)
    monitor.reset()

    await monitor.check(1000, settings())

    assert [(a.level, a.used) for a in alerts] == [("critical", 1000)]


@pytest.mark.parametrize("percent", [1, 80, 100])
def test_the_alert_threshold_is_configurable(percent: int) -> None:
    assert settings(percent=percent).global_alert_percent == percent
