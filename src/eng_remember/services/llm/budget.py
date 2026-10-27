"""Bot-wide model budget: alert before the money runs out, then stop.

The per-learner gate protects one account from overspending. This module protects
the operator from the whole bot: many accounts, a runaway feature, or an attack
that creates new Telegram accounts. It does not decide whether a call is allowed
— the quota gate raises the refusal — but it emits one warning at the configured
percentage and one critical alert when the budget is spent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from eng_remember.config import LlmSettings

__all__ = ["BudgetAlert", "GlobalBudgetMonitor", "global_budget"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BudgetAlert:
    level: str
    used: int
    limit: int


class GlobalBudgetMonitor:
    """Process-local alert state for the bot-wide token budget."""

    def __init__(self) -> None:
        self._notifier: Callable[[BudgetAlert], Awaitable[None]] | None = None
        self._warning_sent = False
        self._critical_sent = False

    def set_notifier(self, notifier: Callable[[BudgetAlert], Awaitable[None]]) -> None:
        self._notifier = notifier

    def reset(self) -> None:
        self._warning_sent = False
        self._critical_sent = False
        self._notifier = None

    async def check(self, used: int, settings: LlmSettings) -> None:
        limit = settings.max_tokens_per_day_global
        if limit <= 0:
            return

        if used >= limit:
            if self._critical_sent:
                return
            self._critical_sent = True
            await self._emit(BudgetAlert("critical", used, limit))
            return

        if used * 100 >= limit * settings.global_alert_percent and not self._warning_sent:
            self._warning_sent = True
            await self._emit(BudgetAlert("warning", used, limit))

    async def _emit(self, alert: BudgetAlert) -> None:
        event = (
            "llm_global_budget_exceeded"
            if alert.level == "critical"
            else "llm_global_budget_warning"
        )
        log.warning(event, used=alert.used, limit=alert.limit)
        if self._notifier is None:
            return
        try:
            await self._notifier(alert)
        except Exception as exc:
            # An alert channel outage must never take down the model path.
            log.warning("llm_budget_alert_failed", error=str(exc))


global_budget = GlobalBudgetMonitor()
