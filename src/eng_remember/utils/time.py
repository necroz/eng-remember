"""Time helpers.

All scheduling happens in UTC. SQLite does not store the offset of a
``DateTime(timezone=True)`` column and hands back naive datetimes, so every
value coming out of the database passes through :func:`ensure_utc` before it
reaches the scheduler — mixing naive and aware datetimes is otherwise the
easiest way to get a silent ``TypeError`` in production.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "add_days",
    "ensure_utc",
    "humanize_interval",
    "local_date",
    "local_day_start",
    "next_local_day_start",
    "parse_clock",
    "resolve_timezone",
    "utcnow",
]


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime; leave aware values in their own zone."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def resolve_timezone(timezone_name: str) -> ZoneInfo:
    """Look up a zone, falling back to UTC.

    An unknown name must never fail a review session: the user typed it into a
    command, and a bad value is a typo, not a reason to lose their queue.
    """
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_date(timezone_name: str, *, now: datetime | None = None) -> date:
    """The calendar date it is right now for the learner.

    Streaks are counted in the user's own days; a learner in Auckland and one in
    Los Angeles must not share a boundary.
    """
    moment = ensure_utc(now) or utcnow()
    return moment.astimezone(resolve_timezone(timezone_name)).date()


def local_day_start(timezone_name: str, *, now: datetime | None = None) -> datetime:
    """UTC timestamp of the most recent midnight in the user's timezone.

    Daily limits are per calendar day for the learner, not for the server, so
    "cards introduced today" has to be anchored to the user's own midnight.
    """
    moment = ensure_utc(now) or utcnow()
    local = moment.astimezone(resolve_timezone(timezone_name))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    # On a DST transition midnight can be ambiguous or nonexistent in some
    # zones; stepping forward to the first valid instant keeps the anchor sane.
    if midnight.utcoffset() is None:  # pragma: no cover - defensive
        midnight = local.replace(hour=1, minute=0, second=0, microsecond=0)
    return ensure_utc(midnight.astimezone(UTC)) or midnight


def next_local_day_start(timezone_name: str, *, now: datetime | None = None) -> datetime:
    """UTC timestamp of the next midnight in the user's timezone.

    A word added today must not come back for review today: its first repetition
    is scheduled for the learner's next calendar day. That is also where the daily
    budget of new cards resets, so a card can never arrive before there is budget
    for it.

    Counted in local calendar days rather than by adding 24 hours: the day a zone
    leaves daylight saving lasts 25 hours, and "plus one day" would still land
    inside it — in the past, which is the queue this is meant to stay out of.
    """
    moment = ensure_utc(now) or utcnow()
    zone = resolve_timezone(timezone_name)
    next_day = moment.astimezone(zone).date() + timedelta(days=1)
    return datetime.combine(next_day, time(0, 0), tzinfo=zone).astimezone(UTC)


def add_days(moment: datetime, days: float) -> datetime:
    return moment + timedelta(days=days)


def parse_clock(value: str) -> time | None:
    """Parse a wall-clock time a user typed: ``9:00``, ``09:00``, ``21:30``.

    Deliberately strict about the shape. "8" and "вечером" are rejected rather
    than guessed at: silently choosing 08:00 for someone who meant 20:00 is worse
    than asking again.
    """
    text = value.strip()
    if ":" not in text:
        return None
    hours_text, _, minutes_text = text.partition(":")
    if not (hours_text.isdigit() and minutes_text.isdigit()):
        return None
    hours, minutes = int(hours_text), int(minutes_text)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return time(hour=hours, minute=minutes)


def humanize_clock(moment: time) -> str:
    return f"{moment.hour:02d}:{moment.minute:02d}"


def humanize_interval(delta_seconds: float) -> str:
    """Render a scheduling interval the way a learner reads it."""
    if delta_seconds < 60:
        return "меньше минуты"
    minutes = delta_seconds / 60
    if minutes < 60:
        return f"{round(minutes)} мин"
    hours = minutes / 60
    if hours < 24:
        return f"{round(hours)} ч"
    days = hours / 24
    if days < 30:
        return f"{round(days)} дн"
    months = days / 30.44
    if months < 12:
        return f"{months:.1f} мес".replace(".0 ", " ")
    return f"{days / 365.25:.1f} г"
