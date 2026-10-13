"""Streak rules, tested with explicit dates and no database."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from eng_remember.config import StreakSettings
from eng_remember.domain.gamification import (
    StreakEventKind,
    StreakState,
    next_milestone,
    record_activity,
    roll_forward,
)

D0 = date(2026, 3, 1)


@pytest.fixture
def settings() -> StreakSettings:
    return StreakSettings(
        freeze_initial=1,
        freeze_max=3,
        freeze_every_days=7,
        milestones=(3, 7, 30),
    )


def days(count: int) -> date:
    return D0 + timedelta(days=count)


def fresh(settings: StreakSettings) -> StreakState:
    return StreakState.fresh(settings)


class TestFirstDay:
    def test_a_new_learner_starts_with_one_freeze(self, settings: StreakSettings) -> None:
        assert fresh(settings).freezes == 1

    def test_the_first_answer_starts_a_streak_of_one(self, settings: StreakSettings) -> None:
        result = record_activity(fresh(settings), D0, settings)

        assert result.state.current == 1
        assert result.state.accounted_on == D0
        assert result.state.longest == 1
        assert result.counted

    def test_answering_again_the_same_day_changes_nothing(self, settings: StreakSettings) -> None:
        once = record_activity(fresh(settings), D0, settings)
        twice = record_activity(once.state, D0, settings)

        assert twice.state == once.state
        assert not twice.counted

    def test_the_streak_counts_days_not_cards(self, settings: StreakSettings) -> None:
        state = fresh(settings)
        for _ in range(10):
            state = record_activity(state, D0, settings).state

        assert state.current == 1


class TestConsecutiveDays:
    def test_a_second_day_extends_the_streak(self, settings: StreakSettings) -> None:
        state = record_activity(fresh(settings), D0, settings).state

        result = record_activity(state, days(1), settings)

        assert result.state.current == 2
        assert result.state.longest == 2

    def test_longest_survives_a_break(self, settings: StreakSettings) -> None:
        state = fresh(settings)
        for offset in range(5):
            state = record_activity(state, days(offset), settings).state
        assert state.longest == 5

        state = record_activity(state, days(20), settings).state

        assert state.current == 1
        assert state.longest == 5

    def test_reaching_a_milestone_is_reported(self, settings: StreakSettings) -> None:
        state = fresh(settings)
        for offset in range(2):
            state = record_activity(state, days(offset), settings).state

        result = record_activity(state, days(2), settings)

        assert result.milestone == 3
        assert result.state.current == 3

    def test_a_non_milestone_day_reports_nothing(self, settings: StreakSettings) -> None:
        state = record_activity(fresh(settings), D0, settings).state

        result = record_activity(state, days(1), settings)

        assert result.milestone is None


class TestMissedDays:
    def test_a_single_missed_day_is_absorbed_by_a_freeze(self, settings: StreakSettings) -> None:
        state = record_activity(fresh(settings), D0, settings).state
        assert state.freezes == 1

        result = record_activity(state, days(2), settings)

        assert result.freezes_used == 1
        assert not result.lost
        assert result.state.current == 2
        assert result.state.freezes == 0
        assert result.state.missed_days == 0

    def test_without_a_freeze_the_streak_breaks(self, settings: StreakSettings) -> None:
        state = fresh(settings)
        state = record_activity(state, D0, settings).state
        # Spend the freeze on an earlier gap.
        state = record_activity(state, days(2), settings).state
        assert state.freezes == 0

        result = record_activity(state, days(5), settings)

        assert result.lost
        assert result.state.current == 1
        assert result.state.missed_days == 2

    def test_freezes_are_consumed_one_per_missed_day(self, settings: StreakSettings) -> None:
        state = StreakState(current=10, longest=10, freezes=3, accounted_on=D0)

        result = record_activity(state, days(4), settings)

        assert result.freezes_used == 3
        assert result.state.freezes == 0
        assert result.state.current == 11
        assert not result.lost

    def test_the_day_that_exhausts_the_freezes_breaks_the_streak(
        self, settings: StreakSettings
    ) -> None:
        state = StreakState(current=10, longest=10, freezes=2, accounted_on=D0)

        result = record_activity(state, days(4), settings)

        assert result.freezes_used == 2
        assert result.lost
        assert result.state.current == 1

    def test_missed_days_are_counted_even_without_a_streak(self, settings: StreakSettings) -> None:
        state = StreakState(current=0, longest=4, freezes=0, missed_days=3, accounted_on=D0)

        result = record_activity(state, days(5), settings)

        assert result.state.missed_days == 7
        assert not result.lost  # nothing was there to lose

    def test_a_long_absence_does_not_loop_over_every_day(self, settings: StreakSettings) -> None:
        """Ten years away must still resolve in one pass."""
        state = StreakState(current=50, longest=50, freezes=3, accounted_on=date(2016, 1, 1))

        result = record_activity(state, date(2026, 1, 1), settings)

        assert result.lost
        assert result.state.current == 1
        assert result.state.missed_days > 3600


class TestFreezeEarning:
    def test_a_freeze_is_earned_on_every_seventh_day(self, settings: StreakSettings) -> None:
        state = StreakState(current=6, longest=6, freezes=0, accounted_on=D0)

        result = record_activity(state, days(1), settings)

        assert result.state.current == 7
        assert result.freeze_earned
        assert result.state.freezes == 1

    def test_earning_stops_at_the_cap(self, settings: StreakSettings) -> None:
        state = StreakState(current=6, longest=6, freezes=3, accounted_on=D0)

        result = record_activity(state, days(1), settings)

        assert result.state.freezes == 3
        assert result.freeze_earned  # the milestone passed even though nothing changed

    def test_a_rebuilt_streak_earns_again(self, settings: StreakSettings) -> None:
        """Otherwise a long absence permanently costs the mechanic."""
        state = StreakState(current=6, longest=20, freezes=0, accounted_on=D0)
        state = record_activity(state, days(1), settings).state
        assert state.freezes == 1

        state = StreakState(current=6, longest=20, freezes=0, accounted_on=days(100))
        result = record_activity(state, days(101), settings)

        assert result.freeze_earned

    def test_earning_can_be_switched_off(self) -> None:
        settings = StreakSettings(freeze_every_days=0, freeze_initial=0)
        state = StreakState(current=6, longest=6, accounted_on=D0)

        result = record_activity(state, days(1), settings)

        assert not result.freeze_earned
        assert result.state.freezes == 0


class TestRollForward:
    def test_reading_the_streak_before_answering_breaks_it(self, settings: StreakSettings) -> None:
        """The user must see a broken streak the moment they open the bot."""
        state = StreakState(current=5, longest=5, freezes=0, accounted_on=D0)

        result = roll_forward(state, days(3), settings)

        assert result.state.current == 0
        assert result.lost
        assert not result.counted

    def test_rolling_forward_the_same_day_is_a_no_op(self, settings: StreakSettings) -> None:
        state = StreakState(current=5, longest=5, accounted_on=D0)

        assert roll_forward(state, D0, settings).state == state

    def test_rolling_forward_never_moves_backwards_in_time(self, settings: StreakSettings) -> None:
        """A timezone move can put "today" before the last activity."""
        state = StreakState(current=5, longest=5, freezes=2, accounted_on=days(5))

        result = roll_forward(state, D0, settings)

        assert result.state == state

    def test_an_untouched_learner_has_nothing_to_roll(self, settings: StreakSettings) -> None:
        assert roll_forward(fresh(settings), D0, settings).state == fresh(settings)

    def test_roll_forward_is_idempotent(self, settings: StreakSettings) -> None:
        state = StreakState(current=5, longest=5, freezes=1, accounted_on=D0)

        once = roll_forward(state, days(3), settings).state
        twice = roll_forward(once, days(3), settings).state

        assert once == twice


class TestNextMilestone:
    def test_the_next_round_number_is_suggested(self, settings: StreakSettings) -> None:
        assert next_milestone(StreakState(current=4), settings) == 7
        assert next_milestone(StreakState(current=0), settings) == 3

    def test_past_the_last_milestone_there_is_none(self, settings: StreakSettings) -> None:
        assert next_milestone(StreakState(current=99), settings) is None


class TestEventDetails:
    def test_a_freeze_event_carries_the_remaining_count(self, settings: StreakSettings) -> None:
        state = StreakState(current=5, longest=5, freezes=2, accounted_on=D0)

        result = record_activity(state, days(3), settings)

        used = [e for e in result.events if e.kind is StreakEventKind.FREEZE_USED]
        assert len(used) == 2
        assert used[0].freezes == 1
        assert used[1].freezes == 0
