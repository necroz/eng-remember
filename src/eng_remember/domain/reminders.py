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

    MORNING = "morning"
    """The queue, sent in the morning slot unless the evening hour has arrived."""

    EVENING = "evening"
    """The rest of the day: whatever is left, plus the streak warning when due."""

    DIGEST = "digest"
    """The weekly summary: takes the morning slot on its configured day."""


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
    digest_enabled: bool = False
    last_digest_on: date | None = None

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

    Two slots a day. In the morning the queue is shown; from the evening hour
    on, one slot owns the rest of the day — it carries what is left and, when
    the streak is at risk, says so in the same message. A morning missed while
    the bot was down is not backfilled after the evening: two messages minutes
    apart would say the same thing twice.
    """
    if not settings.enabled or not context.has_cards:
        return None

    today = local_date(context.timezone, now=now)
    local_clock = now.astimezone(resolve_timezone(context.timezone)).time()

    # The digest closes the morning slot, so it is decided before the evening
    # one: a bot whose first tick came after the evening hour would otherwise
    # skip the week. A digest that lands after the evening hour settles that
    # slot too — it already carries the queue, so no second message follows.
    if _digest_owed(context, today=today, local_clock=local_clock, settings=settings):
        return ReminderKind.DIGEST

    if context.evening_at is not None and local_clock >= context.evening_at:
        return ReminderKind.EVENING if _evening_owed(context, today=today) else None

    # A digest already sent today settles the morning slot, whatever is marked.
    if context.last_digest_on == today:
        return None

    if _slot_closed(context.last_morning_on, today):
        return None
    if local_clock < context.morning_at:
        return None
    if not context.has_work:
        # Nothing to do and nothing to lose: a ping here is pure noise.
        return None
    return ReminderKind.MORNING


def _slot_closed(mark: date | None, today: date) -> bool:
    """Whether a daily mark already fills its slot.

    ``>=`` rather than ``==``: a timezone move west can leave a mark dated after
    the new "today", and the reminder must not be sent twice.
    """
    return mark is not None and mark >= today


def _digest_owed(
    context: ReminderContext,
    *,
    today: date,
    local_clock: time,
    settings: ReminderSettings,
) -> bool:
    return (
        settings.digest_enabled
        and context.digest_enabled
        and today.weekday() == settings.digest_weekday
        and context.last_digest_on != today
        and not _slot_closed(context.last_morning_on, today)
        and local_clock >= context.morning_at
    )


def _evening_owed(context: ReminderContext, *, today: date) -> bool:
    if _slot_closed(context.last_evening_on, today):
        return False
    if context.has_work:
        return True
    return context.streak_alive and not context.answered_today
