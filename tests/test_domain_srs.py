"""Scheduling rules, tested without a database or a bot."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fsrs import Rating, State

from eng_remember.config import SrsSettings
from eng_remember.domain.enums import CardState, ReviewRating
from eng_remember.domain.srs import CardSchedule, SrsScheduler
from eng_remember.utils.time import utcnow


@pytest.fixture
def scheduler() -> SrsScheduler:
    # Fuzzing off: FSRS otherwise scatters intervals by ±5%, which is right in
    # production and impossible to assert on.
    return SrsScheduler(SrsSettings(enable_fuzzing=False))


def test_enums_mirror_the_library(scheduler: SrsScheduler) -> None:
    """Our persisted integers must stay valid inputs for py-fsrs."""
    assert {member.value for member in ReviewRating} == {member.value for member in Rating}
    assert {member.value for member in CardState} == {member.value for member in State}
    assert ReviewRating.AGAIN.value == Rating.Again.value
    assert CardState.REVIEW.value == State.Review.value


def test_new_card_is_due_immediately(scheduler: SrsScheduler) -> None:
    now = utcnow()
    schedule = scheduler.fresh_schedule(now)

    assert schedule.is_due(now)
    assert schedule.is_new
    assert schedule.state is CardState.LEARNING
    assert schedule.reps == 0


def test_a_new_card_can_be_held_back_until_later(scheduler: SrsScheduler) -> None:
    """How "not on the day it was added" is expressed: the caller sets the date."""
    now = utcnow()
    tomorrow = now + timedelta(days=1)

    schedule = scheduler.fresh_schedule(now, due_at=tomorrow)

    assert schedule.due_at == tomorrow
    assert not schedule.is_due(now)
    assert schedule.is_due(tomorrow)
    assert schedule.is_new


def test_first_good_answer_stays_in_learning(scheduler: SrsScheduler) -> None:
    now = utcnow()
    updated = scheduler.review(scheduler.fresh_schedule(now), ReviewRating.GOOD, now=now)

    assert updated.state is CardState.LEARNING
    assert updated.reps == 1
    assert timedelta(minutes=9) < (updated.due_at - now) < timedelta(minutes=11)


def test_easy_moves_straight_to_review(scheduler: SrsScheduler) -> None:
    now = utcnow()
    updated = scheduler.review(scheduler.fresh_schedule(now), ReviewRating.EASY, now=now)

    assert updated.state is CardState.REVIEW
    assert updated.stability is not None and updated.stability > 1
    assert updated.due_at - now > timedelta(days=1)


def test_intervals_grow_with_answers(scheduler: SrsScheduler) -> None:
    now = utcnow()
    schedule = scheduler.fresh_schedule(now)
    intervals: list[float] = []
    for _ in range(4):
        schedule = scheduler.review(schedule, ReviewRating.GOOD, now=schedule.due_at)
        intervals.append((schedule.due_at - now).total_seconds())

    assert intervals == sorted(intervals)
    assert intervals[-1] > intervals[0]


def test_again_after_review_counts_a_lapse(scheduler: SrsScheduler) -> None:
    now = utcnow()
    schedule = scheduler.fresh_schedule(now)
    schedule = scheduler.review(schedule, ReviewRating.EASY, now=now)  # → Review
    lapsed = scheduler.review(schedule, ReviewRating.AGAIN, now=schedule.due_at)

    assert lapsed.state is CardState.RELEARNING
    assert lapsed.lapses == 1
    assert lapsed.reps == schedule.reps + 1


def test_again_before_review_is_not_a_lapse(scheduler: SrsScheduler) -> None:
    """Struggling while still learning is not forgetting something learned."""
    now = utcnow()
    schedule = scheduler.review(scheduler.fresh_schedule(now), ReviewRating.AGAIN, now=now)

    assert schedule.state is CardState.LEARNING
    assert schedule.lapses == 0


def test_retention_setting_changes_the_interval() -> None:
    now = utcnow()
    eager = SrsScheduler(SrsSettings(desired_retention=0.97, enable_fuzzing=False))
    relaxed = SrsScheduler(SrsSettings(desired_retention=0.75, enable_fuzzing=False))

    eager_card = eager.review(eager.fresh_schedule(now), ReviewRating.EASY, now=now)
    relaxed_card = relaxed.review(relaxed.fresh_schedule(now), ReviewRating.EASY, now=now)

    assert eager_card.due_at < relaxed_card.due_at


def test_preview_covers_every_rating_in_order(scheduler: SrsScheduler) -> None:
    now = utcnow()
    previews = scheduler.preview(scheduler.fresh_schedule(now), now=now)

    assert [preview.rating for preview in previews] == list(ReviewRating)
    assert all(preview.interval_seconds > 0 for preview in previews)
    # Easy must never promise a shorter interval than Again.
    assert previews[0].interval_seconds <= previews[-1].interval_seconds


def test_preview_does_not_change_the_card(scheduler: SrsScheduler) -> None:
    now = utcnow()
    schedule = scheduler.review(scheduler.fresh_schedule(now), ReviewRating.GOOD, now=now)
    before = schedule

    scheduler.preview(schedule, now=now)

    assert schedule == before


def test_schedule_round_trips_through_its_fields(scheduler: SrsScheduler) -> None:
    now = utcnow()
    original = scheduler.review(scheduler.fresh_schedule(now), ReviewRating.EASY, now=now)
    rebuilt = CardSchedule(
        due_at=original.due_at,
        state=original.state,
        step=original.step,
        stability=original.stability,
        difficulty=original.difficulty,
        last_review_at=original.last_review_at,
        reps=original.reps,
        lapses=original.lapses,
    )

    assert rebuilt == original


def test_retrievability_drops_between_reviews(scheduler: SrsScheduler) -> None:
    now = utcnow()
    schedule = scheduler.review(scheduler.fresh_schedule(now), ReviewRating.EASY, now=now)

    soon = scheduler.retrievability(schedule, now=schedule.due_at)
    later = scheduler.retrievability(schedule, now=schedule.due_at + timedelta(days=60))

    assert later < soon
    assert 0.0 <= later <= 1.0


def test_naive_datetimes_are_accepted(scheduler: SrsScheduler) -> None:
    """SQLite hands back naive datetimes; scheduling must not explode on them."""
    naive = utcnow().replace(tzinfo=None)
    schedule = CardSchedule.fresh(naive)

    assert schedule.due_at.tzinfo is not None
    updated = scheduler.review(schedule, ReviewRating.GOOD, now=naive)
    assert updated.due_at.tzinfo is not None
