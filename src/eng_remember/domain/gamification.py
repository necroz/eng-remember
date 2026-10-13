"""Streak rules: consecutive days, missed days and freezes.

Pure functions over an immutable state, same shape as :mod:`eng_remember.domain.srs`.
Nothing here reads a clock or touches a database, so every rule — a freeze
absorbing a missed day, a streak breaking, a freeze being earned — is decided by
an ordinary test with an explicit "today".

The day boundary is the learner's own: callers pass a local date, never UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import StrEnum

from eng_remember.config import StreakSettings

__all__ = [
    "StreakEvent",
    "StreakEventKind",
    "StreakState",
    "StreakTransition",
    "next_milestone",
    "record_activity",
    "roll_forward",
]


class StreakEventKind(StrEnum):
    """What happened to the streak while it was brought up to date."""

    DAY_COUNTED = "day_counted"
    FREEZE_EARNED = "freeze_earned"
    FREEZE_USED = "freeze_used"
    STREAK_LOST = "streak_lost"


@dataclass(frozen=True, slots=True)
class StreakEvent:
    """One thing worth telling the user about."""

    kind: StreakEventKind
    streak: int = 0
    freezes: int = 0


@dataclass(frozen=True, slots=True)
class StreakState:
    """Persistable snapshot of a learner's streak."""

    current: int = 0
    longest: int = 0
    freezes: int = 0
    missed_days: int = 0
    # The last local day this state already accounts for. Not "the last day the
    # user answered": after a gap is absorbed this points at the day before
    # today, which is what makes roll_forward idempotent. Read paths (/stats,
    # /start) and the write path both call it, and a read that double-counted a
    # gap would inflate missed_days every time the user opened the bot.
    accounted_on: date | None = None

    @classmethod
    def fresh(cls, settings: StreakSettings) -> StreakState:
        """Starting state for a new learner.

        One freeze up front is deliberate: the mechanic is invisible until it
        saves a streak, and waiting a week for the first one means a week of
        users never finding out it exists.
        """
        return cls(freezes=settings.freeze_initial)

    @property
    def is_alive(self) -> bool:
        return self.current > 0


@dataclass(frozen=True, slots=True)
class StreakTransition:
    """New state plus everything notable that happened on the way."""

    state: StreakState
    events: tuple[StreakEvent, ...] = ()
    milestone: int | None = None

    @property
    def lost(self) -> bool:
        return any(event.kind is StreakEventKind.STREAK_LOST for event in self.events)

    @property
    def freezes_used(self) -> int:
        return sum(1 for event in self.events if event.kind is StreakEventKind.FREEZE_USED)

    @property
    def freeze_earned(self) -> bool:
        return any(event.kind is StreakEventKind.FREEZE_EARNED for event in self.events)

    @property
    def counted(self) -> bool:
        return any(event.kind is StreakEventKind.DAY_COUNTED for event in self.events)


def roll_forward(
    state: StreakState,
    today: date,
    settings: StreakSettings,
) -> StreakTransition:
    """Bring the streak up to date without counting today.

    Called before reading the streak as well as before writing it: a streak that
    broke yesterday should read as broken the moment the user opens the bot, not
    only once they answer a card.
    """
    if state.accounted_on is None or today <= state.accounted_on:
        # Nothing to do. This also covers a clock or timezone move that puts
        # "today" before what was already accounted for — that must not destroy
        # anything.
        return StreakTransition(state=state)

    days_missed = (today - state.accounted_on).days - 1
    if days_missed <= 0:
        return StreakTransition(state=state)

    current = state.current
    freezes = state.freezes
    missed_days = state.missed_days
    events: list[StreakEvent] = []

    for _ in range(days_missed):
        if current == 0:
            # Nothing left to protect; the gap is only recorded.
            missed_days += 1
            continue
        if freezes > 0:
            freezes -= 1
            events.append(
                StreakEvent(kind=StreakEventKind.FREEZE_USED, streak=current, freezes=freezes)
            )
            continue
        events.append(
            StreakEvent(kind=StreakEventKind.STREAK_LOST, streak=current, freezes=freezes)
        )
        current = 0
        missed_days += 1

    return StreakTransition(
        state=replace(
            state,
            current=current,
            freezes=freezes,
            missed_days=missed_days,
            # Everything through yesterday is settled; today is still open.
            accounted_on=today - timedelta(days=1),
        ),
        events=tuple(events),
    )


def record_activity(
    state: StreakState,
    today: date,
    settings: StreakSettings,
) -> StreakTransition:
    """Count today for the streak, absorbing any gap since the last activity.

    Idempotent within a day: answering ten cards counts the day once.
    """
    rolled = roll_forward(state, today, settings)
    current_state = rolled.state
    events = list(rolled.events)

    if current_state.accounted_on == today:
        return StreakTransition(state=current_state, events=tuple(events))

    current = current_state.current + 1
    freezes = current_state.freezes
    earned = _crossed_freeze_threshold(current_state.current, current, settings)
    if earned:
        freezes = min(freezes + 1, settings.freeze_max)

    events.append(StreakEvent(kind=StreakEventKind.DAY_COUNTED, streak=current, freezes=freezes))
    if earned:
        events.append(
            StreakEvent(kind=StreakEventKind.FREEZE_EARNED, streak=current, freezes=freezes)
        )

    return StreakTransition(
        state=replace(
            current_state,
            current=current,
            longest=max(current_state.longest, current),
            freezes=freezes,
            accounted_on=today,
        ),
        events=tuple(events),
        milestone=current if current in settings.milestones else None,
    )


def next_milestone(state: StreakState, settings: StreakSettings) -> int | None:
    """The next round number the streak can reach, for progress hints."""
    for milestone in settings.milestones:
        if milestone > state.current:
            return milestone
    return None


def _crossed_freeze_threshold(before: int, after: int, settings: StreakSettings) -> bool:
    """A freeze is earned when the streak passes a multiple of the interval.

    Comparing quotients rather than testing ``after % n == 0`` means a streak
    that breaks and rebuilds earns another freeze on the way back up — which is
    what keeps the mechanic useful after a long absence.
    """
    if settings.freeze_every_days <= 0:
        return False
    return after // settings.freeze_every_days > before // settings.freeze_every_days
