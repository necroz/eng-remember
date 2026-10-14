"""Shared answer-history aggregation for the digest and the statistics screen.

One place builds the 28-day series, so the heatmap in a Monday message and the
one under `/stats` can never disagree.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from eng_remember.domain.digest import HEATMAP_DAYS, daily_series
from eng_remember.models.user import User
from eng_remember.repositories.reviews import ReviewRepository
from eng_remember.utils.time import ensure_utc, local_date, local_day_start, utcnow

__all__ = ["heatmap_days"]


async def heatmap_days(
    reviews: ReviewRepository,
    user: User,
    *,
    now: datetime | None = None,
) -> list[int]:
    """Answer counts for the last 28 local days, oldest first, gaps as zeroes."""
    moment = ensure_utc(now) or utcnow()
    day_start = local_day_start(user.timezone, now=moment)
    history = await reviews.history_since(
        user.id, since=day_start - timedelta(days=HEATMAP_DAYS - 1)
    )

    by_day: dict[date, int] = {}
    for review in history:
        reviewed_at = ensure_utc(review.reviewed_at) or review.reviewed_at
        day = local_date(user.timezone, now=reviewed_at)
        by_day[day] = by_day.get(day, 0) + 1

    return daily_series(
        by_day,
        end=local_date(user.timezone, now=moment),
        length=HEATMAP_DAYS,
    )
