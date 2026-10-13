"""When to nudge a learner, decided without a clock or a database.

The whole scheduling rule is one function over an immutable context, so "the
bot was down all morning and started at ten" and "the evening warning fires only
when the streak is really at risk" are ordinary tests with an explicit moment.

Times are the learner's own wall-clock times. A reminder set for 09:00 keeps
meaning 09:00 across a daylight-saving change, because the comparison is against
the local clock, not against a fixed offset — which is also why the interval
between two reminders is not exactly 24 hours twice a year.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum

from eng_remember.config import ReminderSettings
from eng_remember.utils.time import local_date, resolve_timezone

__all__ = ["ReminderContext", "ReminderKind", "decide"]


class ReminderKind(StrEnum):
    """What the reminder is for. Also the key for the daily "already sent" marks."""

    QUEUE = "queue"
    """There is something to review."""

    STREAK = "streak"
    """The streak is alive and today has not been counted yet."""


@dataclass(frozen=True, slots=True)
class ReminderContext:
    """Everything the decision depends on, gathered by the caller."""

    timezone: str
    morning_at: time
    evening_at: time | None
    last_morning_on: date | None
    last_evening_on: date | None
    has_cards: bool
    due_now: int
    new_available: int
    answered_today: bool
    streak_alive: bool
    streak_days: int

    @property
    def has_work(self) -> bool:
        return self.due_now > 0 or self.new_available > 0


def decide(
    context: ReminderContext,
    *,
    now: datetime,
    settings: ReminderSettings,
) -> ReminderKind | None:
    """Which reminder, if any, this learner is owed right now.

    Order matters. The streak warning is checked first and is allowed to fire
    after a morning reminder, because it carries different information — the
    morning one says what to do, the evening one says what is about to be lost.
    It is also checked first so that a bot that was down all day sends one
    message in the evening rather than both at once.
    """
    if not settings.enabled or not context.has_cards:
        return None

    today = local_date(context.timezone, now=now)
    local_clock = now.astimezone(resolve_timezone(context.timezone)).time()

    if _streak_is_at_risk(context, today=today, clock=local_clock):
        return ReminderKind.STREAK

    if context.last_morning_on == today:
        return None
    if local_clock < context.morning_at:
        return None
    if not context.has_work:
        # Nothing to do and nothing to lose: a ping here is pure noise.
        return None
    return ReminderKind.QUEUE


def _streak_is_at_risk(
    context: ReminderContext,
    *,
    today: date,
    clock: time,
) -> bool:
    return (
        context.evening_at is not None
        and context.last_evening_on != today
        and clock >= context.evening_at
        and context.streak_alive
        and not context.answered_today
    )


def covers_morning(kind: ReminderKind) -> bool:
    """Whether sending this reminder also settles the morning slot.

    The streak warning already carries the queue, so a learner who gets it must
    not also get the queue reminder later the same day.
    """
    return kind is ReminderKind.STREAK
