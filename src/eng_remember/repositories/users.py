from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.domain.reminders import ReminderKind
from eng_remember.models.card import Card
from eng_remember.models.user import User

__all__ = ["TelegramProfile", "UserRepository"]


@dataclass(frozen=True, slots=True)
class TelegramProfile:
    """The subset of a Telegram account this project cares about."""

    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    language_code: str | None = None


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        stmt = select(User).where(User.telegram_id == telegram_id)
        return await self._session.scalar(stmt)

    async def get_or_create(
        self,
        profile: TelegramProfile,
        *,
        default_timezone: str = "UTC",
        new_cards_per_day: int = 20,
    ) -> tuple[User, bool]:
        """Return the user for this Telegram account, creating it on first contact.

        Returns a ``(user, created)`` pair so callers can send a first-time
        greeting without a second query.
        """
        existing = await self.get_by_telegram_id(profile.telegram_id)
        if existing is not None:
            self._refresh_profile(existing, profile)
            return existing, False

        user = User(
            telegram_id=profile.telegram_id,
            username=profile.username,
            first_name=profile.first_name,
            language_code=profile.language_code,
            timezone=default_timezone,
            new_cards_per_day=new_cards_per_day,
        )
        self._session.add(user)
        await self._session.flush()
        return user, True

    async def list_reminder_candidates(
        self,
        *,
        utc_today: date,
        after_id: int = 0,
        limit: int,
    ) -> list[User]:
        """Who might be owed a reminder today.

        The date filter is a deliberate superset: a learner who already got
        today's message has ``last_reminder_on`` equal to their local date, which
        is never earlier than the UTC date, so ``<= utc_today`` excludes exactly
        those who are done and nobody else. Narrowing further — the learner's own
        clock — happens in Python, because "is it past 09:00 for this user" needs
        their timezone and cannot be written portably in SQL.

        Because the filter self-narrows, the limit only bounds one tick's work; it
        can never hide a user forever, which a plain "first N users" page would.

        ``after_id`` walks the list with a cursor instead of re-reading the same
        head every time: a learner whose reminder failed mid-tick stays a
        candidate, and without a cursor the loop recalling the same batch would
        never end.
        """
        stmt = (
            select(User)
            .where(
                User.id > after_id,
                User.reminders_enabled.is_(True),
                User.is_blocked.is_(False),
                or_(User.last_reminder_on.is_(None), User.last_reminder_on <= utc_today),
                exists(select(Card.id).where(Card.user_id == User.id)),
            )
            .order_by(User.id)
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())

    async def record_reminder(
        self,
        user: User,
        *,
        kind: ReminderKind,
        on: date,
    ) -> None:
        """Mark the slot as used for that local day.

        The evening message also settles the morning slot: it already carries
        the queue, so following it with "you have cards to review" would be
        nagging.
        """
        if kind is ReminderKind.EVENING:
            user.last_evening_reminder_on = on
            user.last_reminder_on = on
        else:
            user.last_reminder_on = on
        await self._session.flush()

    async def touch(self, user: User, *, when: datetime) -> None:
        user.last_seen_at = when

    async def set_blocked(self, user: User, *, blocked: bool) -> None:
        user.is_blocked = blocked

    @staticmethod
    def _refresh_profile(user: User, profile: TelegramProfile) -> None:
        """Keep the display fields current; users rename themselves often."""
        user.username = profile.username
        user.first_name = profile.first_name
        user.language_code = profile.language_code
