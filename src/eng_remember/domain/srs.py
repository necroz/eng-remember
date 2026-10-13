"""Spaced-repetition scheduling on top of py-fsrs.

This is the only module that knows the shape of the FSRS library. It exposes a
plain dataclass (:class:`CardSchedule`) that maps one-to-one onto the scheduling
columns of the ``cards`` table, so persistence code never touches py-fsrs types
and scheduling tests never need a database.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from fsrs import Card, Rating, Scheduler, State

from eng_remember.config import SrsSettings
from eng_remember.domain.enums import CardState, ReviewRating
from eng_remember.utils.time import ensure_utc, utcnow

__all__ = ["CardSchedule", "SchedulePreview", "SrsScheduler"]

_RATING_TO_FSRS: dict[ReviewRating, Rating] = {
    ReviewRating.AGAIN: Rating.Again,
    ReviewRating.HARD: Rating.Hard,
    ReviewRating.GOOD: Rating.Good,
    ReviewRating.EASY: Rating.Easy,
}

_STATE_FROM_FSRS: dict[State, CardState] = {
    State.Learning: CardState.LEARNING,
    State.Review: CardState.REVIEW,
    State.Relearning: CardState.RELEARNING,
}

_STATE_TO_FSRS: dict[CardState, State] = {v: k for k, v in _STATE_FROM_FSRS.items()}


@dataclass(frozen=True, slots=True)
class CardSchedule:
    """Persistable snapshot of a card's memory state."""

    due_at: datetime
    state: CardState
    step: int | None = None
    stability: float | None = None
    difficulty: float | None = None
    last_review_at: datetime | None = None
    reps: int = 0
    lapses: int = 0

    @classmethod
    def fresh(cls, now: datetime | None = None) -> CardSchedule:
        """Schedule for a card that has never been reviewed."""
        return cls(due_at=ensure_utc(now) or utcnow(), state=CardState.LEARNING)

    def is_due(self, now: datetime | None = None) -> bool:
        moment = ensure_utc(now) or utcnow()
        return self.due_at <= moment

    @property
    def is_new(self) -> bool:
        return self.reps == 0


@dataclass(frozen=True, slots=True)
class SchedulePreview:
    """What each answer button would do, for the UI."""

    rating: ReviewRating
    due_at: datetime
    interval_seconds: float


class SrsScheduler:
    """Thin, stateless wrapper around :class:`fsrs.Scheduler`."""

    def __init__(self, settings: SrsSettings) -> None:
        self._settings = settings
        self._scheduler = Scheduler(
            desired_retention=settings.desired_retention,
            learning_steps=[timedelta(minutes=m) for m in settings.learning_steps_minutes],
            relearning_steps=[timedelta(minutes=m) for m in settings.relearning_steps_minutes],
            maximum_interval=settings.maximum_interval_days,
            enable_fuzzing=settings.enable_fuzzing,
        )

    @property
    def settings(self) -> SrsSettings:
        return self._settings

    def fresh_schedule(self, now: datetime | None = None) -> CardSchedule:
        """Schedule for a card that has just been created — due immediately."""
        return CardSchedule.fresh(now)

    def review(
        self,
        schedule: CardSchedule,
        rating: ReviewRating,
        *,
        now: datetime | None = None,
    ) -> CardSchedule:
        """Apply an answer and return the next schedule.

        Pure function of ``(schedule, rating, now)`` — no hidden clock, which is
        what lets the tests assert exact intervals.
        """
        moment = ensure_utc(now) or utcnow()
        card, _log = self._scheduler.review_card(
            self._to_fsrs_card(schedule),
            _RATING_TO_FSRS[rating],
            review_datetime=moment,
        )
        was_lapse = rating is ReviewRating.AGAIN and schedule.state is CardState.REVIEW
        return CardSchedule(
            due_at=ensure_utc(card.due) or moment,
            state=_STATE_FROM_FSRS[card.state],
            step=card.step,
            stability=card.stability,
            difficulty=card.difficulty,
            last_review_at=ensure_utc(card.last_review) or moment,
            reps=schedule.reps + 1,
            lapses=schedule.lapses + (1 if was_lapse else 0),
        )

    def preview(
        self,
        schedule: CardSchedule,
        *,
        now: datetime | None = None,
    ) -> list[SchedulePreview]:
        """Interval each of the four answers would produce, for button hints."""
        moment = ensure_utc(now) or utcnow()
        previews: list[SchedulePreview] = []
        for rating in ReviewRating:
            next_schedule = self.review(schedule, rating, now=moment)
            previews.append(
                SchedulePreview(
                    rating=rating,
                    due_at=next_schedule.due_at,
                    interval_seconds=(next_schedule.due_at - moment).total_seconds(),
                )
            )
        return previews

    def retrievability(
        self,
        schedule: CardSchedule,
        *,
        now: datetime | None = None,
    ) -> float:
        """Probability the learner still remembers the word right now."""
        moment = ensure_utc(now) or utcnow()
        if schedule.stability is None:
            return 0.0
        return float(self._scheduler.get_card_retrievability(self._to_fsrs_card(schedule), moment))

    @staticmethod
    def _to_fsrs_card(schedule: CardSchedule) -> Card:
        return Card(
            state=_STATE_TO_FSRS[schedule.state],
            step=schedule.step,
            stability=schedule.stability,
            difficulty=schedule.difficulty,
            due=schedule.due_at,
            last_review=schedule.last_review_at,
        )

    @staticmethod
    def with_due(schedule: CardSchedule, due_at: datetime) -> CardSchedule:
        """Postpone a card without recording an answer (used by 'later')."""
        return replace(schedule, due_at=due_at)
