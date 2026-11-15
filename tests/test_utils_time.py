"""Time helpers: the boundaries a learner actually sees and the day anchor."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from eng_remember.utils.time import humanize_interval, local_day_start


def test_humanize_interval_floors_at_the_boundaries() -> None:
    """Just under an hour is 59 minutes, not a rounded-up 60 minutes."""
    assert humanize_interval(3599) == "59 мин"
    assert humanize_interval(3576) == "59 мин"  # 59.6 minutes
    assert humanize_interval(86399) == "23 ч"
    assert humanize_interval(23.6 * 3600) == "23 ч"


def test_humanize_interval_rounds_everything_else_down() -> None:
    assert humanize_interval(30) == "меньше минуты"
    assert humanize_interval(60) == "1 мин"
    assert humanize_interval(90) == "1 мин"
    assert humanize_interval(3600) == "1 ч"
    assert humanize_interval(86400) == "1 дн"


def test_humanize_interval_floors_months_and_years() -> None:
    assert humanize_interval(350 * 86400) == "11 мес"
    assert humanize_interval(400 * 86400) == "1 г"
    assert humanize_interval(800 * 86400) == "2 г"


def test_local_day_start_is_the_first_valid_instant_of_the_day() -> None:
    """Santiago starts DST at 00:00: that midnight does not exist, 01:00 does.

    The anchor must still be the start of the learner's calendar day rather than
    an exception or a value from the previous day.
    """
    moment = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

    start = local_day_start("America/Santiago", now=moment)

    assert start == datetime(2026, 9, 6, 4, 0, tzinfo=UTC)
    assert start.astimezone(ZoneInfo("America/Santiago")).hour == 1
