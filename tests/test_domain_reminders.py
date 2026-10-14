"""Who gets reminded when — decided with an explicit clock, no database.

Two slots a day: the morning one shows the queue, the evening one owns the rest
of the day — it carries whatever is left and, when the streak is at risk, says
so in the same message.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from eng_remember.config import ReminderSettings
from eng_remember.domain.reminders import (
    ReminderContext,
    ReminderKind,
    covers_morning,
    decide,
)


def at(hour: int, minute: int = 0, *, tz: str = "UTC") -> datetime:
    """A moment expressed in UTC that reads as the given local clock in ``tz``."""
    from zoneinfo import ZoneInfo

    return datetime(2026, 5, 14, hour, minute, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def monday(hour: int, minute: int = 0, *, tz: str = "UTC") -> datetime:
    """2026-05-11 is a Monday."""
    from zoneinfo import ZoneInfo

    return datetime(2026, 5, 11, hour, minute, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def context(**overrides: object) -> ReminderContext:
    base: dict[str, object] = {
        "timezone": "UTC",
        "morning_at": time(9, 0),
        "evening_at": time(20, 0),
        "last_morning_on": None,
        "last_evening_on": None,
        "has_cards": True,
        "due_now": 5,
        "new_available": 0,
        "answered_today": False,
        "streak_alive": True,
        "streak_days": 7,
    }
    base.update(overrides)
    return ReminderContext(**base)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> ReminderSettings:
    return ReminderSettings()


class TestMorning:
    def test_before_the_hour_nothing_is_sent(self, settings: ReminderSettings) -> None:
        assert decide(context(), now=at(8, 59), settings=settings) is None

    def test_the_queue_reminder_fires_at_the_hour(self, settings: ReminderSettings) -> None:
        assert decide(context(), now=at(9, 0), settings=settings) is ReminderKind.MORNING

    def test_it_fires_late_if_the_bot_was_down(self, settings: ReminderSettings) -> None:
        """Catch-up, not exact-time scheduling: 10:00 is fine for a 09:00 slot."""
        assert decide(context(), now=at(10, 30), settings=settings) is ReminderKind.MORNING

    def test_only_once_a_day(self, settings: ReminderSettings) -> None:
        already = context(last_morning_on=date(2026, 5, 14))

        assert decide(already, now=at(12, 0), settings=settings) is None

    def test_yesterday_does_not_block_today(self, settings: ReminderSettings) -> None:
        yesterday = context(last_morning_on=date(2026, 5, 13))

        assert decide(yesterday, now=at(9, 0), settings=settings) is ReminderKind.MORNING

    def test_nothing_to_do_means_no_message(self, settings: ReminderSettings) -> None:
        """ "You have nothing to review" is a notification about nothing."""
        idle = context(due_now=0, new_available=0)

        assert decide(idle, now=at(9, 0), settings=settings) is None

    def test_new_cards_alone_are_reason_enough(self, settings: ReminderSettings) -> None:
        fresh = context(due_now=0, new_available=4)

        assert decide(fresh, now=at(9, 0), settings=settings) is ReminderKind.MORNING

    def test_a_learner_without_cards_is_left_alone(self, settings: ReminderSettings) -> None:
        empty = context(has_cards=False, due_now=0, new_available=0)

        assert decide(empty, now=at(9, 0), settings=settings) is None

    def test_the_kill_switch_stops_everything(self) -> None:
        off = ReminderSettings(enabled=False)

        assert decide(context(), now=at(9, 0), settings=off) is None

    def test_the_evening_slot_owns_the_rest_of_the_day(self, settings: ReminderSettings) -> None:
        """A morning that was never sent is not backfilled after the evening."""
        missed = context(last_evening_on=date(2026, 5, 14))

        assert decide(missed, now=at(21, 0), settings=settings) is None


class TestEvening:
    def test_the_remainder_fires_even_after_a_morning_reminder(
        self, settings: ReminderSettings
    ) -> None:
        reminded = context(last_morning_on=date(2026, 5, 14))

        assert decide(reminded, now=at(20, 0), settings=settings) is ReminderKind.EVENING

    def test_the_remainder_fires_when_the_day_is_already_counted(
        self, settings: ReminderSettings
    ) -> None:
        answered_with_work_left = context(
            due_now=3,
            answered_today=True,
            last_morning_on=date(2026, 5, 14),
        )

        assert (
            decide(answered_with_work_left, now=at(20, 30), settings=settings)
            is ReminderKind.EVENING
        )

    def test_the_streak_warning_fires_with_an_empty_queue(self, settings: ReminderSettings) -> None:
        no_work = context(due_now=0, new_available=0)

        assert decide(no_work, now=at(20, 0), settings=settings) is ReminderKind.EVENING

    def test_total_quiet_when_nothing_to_do_and_nothing_to_lose(
        self, settings: ReminderSettings
    ) -> None:
        done = context(
            due_now=0,
            new_available=0,
            answered_today=True,
            last_morning_on=date(2026, 5, 14),
        )

        assert decide(done, now=at(20, 30), settings=settings) is None

    def test_quiet_without_a_streak_and_without_work(self, settings: ReminderSettings) -> None:
        no_streak = context(due_now=0, new_available=0, streak_alive=False)

        assert decide(no_streak, now=at(20, 30), settings=settings) is None

    def test_work_alone_still_rings_without_a_streak(self, settings: ReminderSettings) -> None:
        no_streak = context(streak_alive=False)

        assert decide(no_streak, now=at(20, 30), settings=settings) is ReminderKind.EVENING

    def test_it_fires_only_once(self, settings: ReminderSettings) -> None:
        already = context(
            last_morning_on=date(2026, 5, 14),
            last_evening_on=date(2026, 5, 14),
        )

        assert decide(already, now=at(22, 0), settings=settings) is None

    def test_it_can_be_switched_off_per_user(self, settings: ReminderSettings) -> None:
        without_evening = context(evening_at=None, last_morning_on=date(2026, 5, 14))

        assert decide(without_evening, now=at(21, 0), settings=settings) is None

    def test_without_the_evening_slot_the_morning_owns_the_whole_day(
        self, settings: ReminderSettings
    ) -> None:
        without_evening = context(evening_at=None)

        assert decide(without_evening, now=at(21, 0), settings=settings) is ReminderKind.MORNING

    def test_a_bot_down_all_day_sends_one_message_not_two(self, settings: ReminderSettings) -> None:
        """Both slots are pending at 22:00; the evening one carries the queue too."""
        assert decide(context(), now=at(22, 0), settings=settings) is ReminderKind.EVENING

    def test_after_a_long_outage_without_a_streak_the_remainder_still_goes(
        self, settings: ReminderSettings
    ) -> None:
        stale = context(streak_alive=False)

        assert decide(stale, now=at(22, 0), settings=settings) is ReminderKind.EVENING


class TestDigest:
    def test_it_takes_the_morning_slot_on_its_day(self, settings: ReminderSettings) -> None:
        digest = context(digest_enabled=True)

        assert decide(digest, now=monday(9, 0), settings=settings) is ReminderKind.DIGEST

    def test_other_days_keep_the_ordinary_morning(self, settings: ReminderSettings) -> None:
        digest = context(digest_enabled=True)

        assert decide(digest, now=at(9, 0), settings=settings) is ReminderKind.MORNING

    def test_switched_off_keeps_the_ordinary_morning(self, settings: ReminderSettings) -> None:
        assert (
            decide(context(digest_enabled=False), now=monday(9, 0), settings=settings)
            is ReminderKind.MORNING
        )

    def test_it_fires_with_an_empty_queue(self, settings: ReminderSettings) -> None:
        """The digest is worth reading even after a quiet week."""
        idle = context(digest_enabled=True, due_now=0, new_available=0)

        assert decide(idle, now=monday(9, 0), settings=settings) is ReminderKind.DIGEST

    def test_not_before_the_hour(self, settings: ReminderSettings) -> None:
        assert decide(context(digest_enabled=True), now=monday(8, 30), settings=settings) is None

    def test_only_once(self, settings: ReminderSettings) -> None:
        done = context(digest_enabled=True, last_digest_on=date(2026, 5, 11))

        assert decide(done, now=monday(10, 0), settings=settings) is None

    def test_a_custom_weekday(self) -> None:
        thursday = ReminderSettings(digest_weekday=3)

        assert (
            decide(context(digest_enabled=True), now=at(9, 0), settings=thursday)
            is ReminderKind.DIGEST
        )

    def test_it_covers_the_morning(self) -> None:
        assert covers_morning(ReminderKind.DIGEST) is True

    def test_the_evening_is_not_taken_over(self, settings: ReminderSettings) -> None:
        done = context(
            digest_enabled=True,
            last_digest_on=date(2026, 5, 11),
            last_morning_on=date(2026, 5, 11),
        )

        assert decide(done, now=monday(20, 0), settings=settings) is ReminderKind.EVENING


class TestTimezones:
    def test_the_hour_is_the_learners_own(self, settings: ReminderSettings) -> None:
        """09:00 in Berlin is 07:00 UTC, so a UTC learner is still asleep."""
        berlin = context(timezone="Europe/Berlin")

        assert decide(berlin, now=at(7, 0, tz="UTC"), settings=settings) is not None
        assert decide(context(timezone="UTC"), now=at(7, 0), settings=settings) is None

    def test_the_daily_mark_is_the_learners_own_day(self, settings: ReminderSettings) -> None:
        """The "already sent today" mark expires on the learner's midnight, not UTC's."""
        # 21:30 UTC on the 14th is 09:30 on the 15th in Auckland, so a mark left
        # on the 14th must not block the reminder — even though UTC still calls
        # it the 14th, and a UTC learner with that same mark is correctly skipped.
        # The evening slot is off so this isolates the morning mark.
        marked = {"last_morning_on": date(2026, 5, 14), "evening_at": None}
        auckland = context(timezone="Pacific/Auckland", **marked)
        utc_learner = context(timezone="UTC", **marked)

        assert decide(auckland, now=at(21, 30), settings=settings) is ReminderKind.MORNING
        assert decide(utc_learner, now=at(21, 30), settings=settings) is None

    def test_an_unknown_timezone_falls_back_to_utc(self, settings: ReminderSettings) -> None:
        broken = context(timezone="Mars/Olympus")

        assert decide(broken, now=at(9, 0), settings=settings) is ReminderKind.MORNING


class TestAroundMidnight:
    def test_a_morning_slot_just_after_midnight_works(self) -> None:
        early = ReminderSettings(default_time=time(0, 30))
        context_at_midnight = context(morning_at=time(0, 30), evening_at=None)

        assert decide(context_at_midnight, now=at(0, 30), settings=early) is ReminderKind.MORNING

    def test_a_reminder_set_for_later_today_is_not_early(self, settings: ReminderSettings) -> None:
        late = context(morning_at=time(23, 0), evening_at=None)

        assert decide(late, now=at(22, 59), settings=settings) is None
        assert decide(late, now=at(23, 0), settings=settings) is ReminderKind.MORNING

    def test_ordering_of_the_two_slots_does_not_matter(self, settings: ReminderSettings) -> None:
        """Someone who sets the evening before the morning ping gets one
        message, not two — the evening slot owns everything after its hour."""
        odd = context(morning_at=time(21, 0), evening_at=time(8, 0))

        assert decide(odd, now=at(21, 0), settings=settings) is ReminderKind.EVENING
