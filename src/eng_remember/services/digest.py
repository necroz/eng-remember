"""Shared answer-history aggregation for the digest and the statistics screen.

One place builds the 28-day series, so the heatmap in a Monday message and the
one under `/stats` can never disagree.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

from eng_remember.domain.digest import HEATMAP_DAYS, daily_series
from eng_remember.models.review import Review
from eng_remember.models.user import User
from eng_remember.repositories.reviews import ReviewRepository
from eng_remember.utils.time import ensure_utc, local_date, resolve_timezone, utcnow

__all__ = ["bucket_by_local_day", "heatmap_days", "local_midnight"]


def local_midnight(timezone: str, day: date) -> datetime:
    """The UTC instant of the given local day's midnight.

    Built from the calendar date and the zone, not by subtracting 24-hour
    multiples: across a DST transition a day is 23 or 25 hours long, and only
    the date knows where it actually starts.
    """
    zone = resolve_timezone(timezone)
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)


def bucket_by_local_day(reviews: Sequence[Review], timezone: str) -> dict[date, int]:
    """Answer counts keyed by the learner's own calendar date."""
    by_day: dict[date, int] = {}
    for review in reviews:
        reviewed_at = ensure_utc(review.reviewed_at) or review.reviewed_at
        day = local_date(timezone, now=reviewed_at)
        by_day[day] = by_day.get(day, 0) + 1
    return by_day


async def heatmap_days(
    reviews: ReviewRepository,
    user: User,
    *,
    now: datetime | None = None,
) -> list[int]:
    """Answer counts for the last 28 local days, oldest first, gaps as zeroes."""
    moment = ensure_utc(now) or utcnow()
    today = local_date(user.timezone, now=moment)
    oldest = today - timedelta(days=HEATMAP_DAYS - 1)
    history = await reviews.history_since(user.id, since=local_midnight(user.timezone, oldest))
    by_day = bucket_by_local_day(history, user.timezone)
    return daily_series(by_day, end=today, length=HEATMAP_DAYS)
