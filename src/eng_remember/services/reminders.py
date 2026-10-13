"""Sending reminders on a schedule.

One tick asks "who is owed a message right now", sends it, and records the
watermark. The decision itself lives in :mod:`eng_remember.domain.reminders`;
this module gathers what it needs and deals with the consequences — a learner who
blocked the bot, a delivery that failed, a batch too large for one tick.

Telegram is reached only through the :class:`Notifier` protocol, so the whole
thing is testable without a bot and an operator can drive it from cron instead of
from the in-process loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.config import ReminderSettings, Settings
from eng_remember.domain.gamification import StreakState, roll_forward
from eng_remember.domain.reminders import ReminderContext, ReminderKind, decide
from eng_remember.errors import RecipientUnreachable
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardRepository
from eng_remember.repositories.reviews import ReviewRepository
from eng_remember.repositories.streaks import StreakRepository
from eng_remember.repositories.users import UserRepository
from eng_remember.utils.time import ensure_utc, local_date, local_day_start, utcnow

__all__ = ["Notifier", "ReminderPlan", "ReminderRun", "ReminderService", "build_reminder_service"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReminderPlan:
    """A reminder that is about to be sent, with everything it needs to read well."""

    user: User
    kind: ReminderKind
    local_day: date
    due_now: int
    new_available: int
    streak_days: int


class Notifier(Protocol):
    """Delivering one message, and saying when delivery is impossible.

    Raising :class:`RecipientUnreachable` rather than returning a flag keeps the
    caller's options explicit: an unreachable account is retired, a transport
    failure is retried later, and neither can be mistaken for the other. Rendering
    is the notifier's job, so this service never touches user-facing text.
    """

    async def send_reminder(self, plan: ReminderPlan) -> None: ...


@dataclass(slots=True)
class ReminderRun:
    """What one tick did, for the log line and for tests."""

    candidates: int = 0
    sent: list[ReminderPlan] = field(default_factory=list)
    unreachable: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)

    @property
    def sent_count(self) -> int:
        return len(self.sent)


class ReminderService:
    def __init__(
        self,
        *,
        users: UserRepository,
        cards: CardRepository,
        reviews: ReviewRepository,
        streaks: StreakRepository,
        notifier: Notifier,
        settings: Settings,
    ) -> None:
        self._users = users
        self._cards = cards
        self._reviews = reviews
        self._streaks = streaks
        self._notifier = notifier
        self._reminders: ReminderSettings = settings.reminders
        self._streak_settings = settings.streak

    async def run(self, *, now: datetime | None = None) -> ReminderRun:
        """One pass over everyone who might be owed a reminder."""
        run = ReminderRun()
        if not self._reminders.enabled:
            return run

        moment = ensure_utc(now) or utcnow()
        utc_today = local_date("UTC", now=moment)
        cursor = 0

        # Walks every candidate, chunk by chunk. Deliberately no ceiling: the
        # walk always restarts from the lowest id, so a cap would permanently
        # starve accounts past it. The price is that learners who already had
        # today's reminder are looked at again and skipped by the decision —
        # cheaper than a per-timezone "next reminder at" column, and the reason
        # that column is noted as the next optimisation rather than done now.
        while True:
            batch = await self._users.list_reminder_candidates(
                utc_today=utc_today,
                after_id=cursor,
                limit=self._reminders.batch_size,
            )
            if not batch:
                break
            cursor = batch[-1].id
            run.candidates += len(batch)

            for plan in await self._plan(batch, now=moment):
                await self._deliver(plan, run)

        if run.sent_count or run.failed or run.unreachable:
            log.info(
                "reminder_tick",
                candidates=run.candidates,
                sent=run.sent_count,
                unreachable=len(run.unreachable),
                failed=len(run.failed),
            )
        else:
            # Every ten minutes "nothing to do" at INFO would drown the log.
            log.debug("reminder_tick_idle", candidates=run.candidates)
        return run

    async def _plan(self, batch: list[User], *, now: datetime) -> list[ReminderPlan]:
        """Decide the whole batch, with the counting queries batched as well."""
        user_ids = [user.id for user in batch]
        counts = await self._cards.queue_counts(user_ids, now=now)
        streak_states = await self._streaks.by_user_ids(user_ids)
        answered = await self._answered_today(batch, now=now)

        plans: list[ReminderPlan] = []
        today_by_user = {user.id: local_date(user.timezone, now=now) for user in batch}

        for user in batch:
            queue = counts.get(user.id)
            if queue is None:  # pragma: no cover - the query returns every id
                continue

            # Settled in memory only. A tick must not write to the streak just to
            # look at it, and the answer and statistics paths persist the same
            # result whenever the learner next shows up.
            streak = roll_forward(
                streak_states.get(user.id, StreakState()),
                today_by_user[user.id],
                self._streak_settings,
            )

            context = ReminderContext(
                timezone=user.timezone,
                morning_at=user.reminder_time or self._reminders.default_time,
                evening_at=_evening_at(user, self._reminders),
                last_morning_on=user.last_reminder_on,
                last_evening_on=user.last_evening_reminder_on,
                # The candidate query already required at least one card, so this
                # is a guarantee rather than a fresh lookup.
                has_cards=True,
                due_now=queue.due,
                new_available=queue.new,
                answered_today=answered.get(user.id, 0) > 0,
                streak_alive=streak.state.is_alive,
                streak_days=streak.state.current,
            )
            kind = decide(context, now=now, settings=self._reminders)
            if kind is None:
                continue
            plans.append(
                ReminderPlan(
                    user=user,
                    kind=kind,
                    local_day=today_by_user[user.id],
                    due_now=queue.due,
                    new_available=queue.new,
                    streak_days=streak.state.current,
                )
            )
        return plans

    async def _answered_today(self, batch: list[User], *, now: datetime) -> dict[int, int]:
        """Answers per learner since their own midnight.

        Users are grouped by day boundary first: a whole batch usually spans two
        or three distinct midnights, so this stays a couple of queries instead of
        one per learner.
        """
        by_boundary: dict[datetime, list[int]] = {}
        for user in batch:
            boundary = local_day_start(user.timezone, now=now)
            by_boundary.setdefault(boundary, []).append(user.id)

        answered: dict[int, int] = {}
        for boundary, user_ids in by_boundary.items():
            answered.update(await self._reviews.counts_since(user_ids, since=boundary))
        return answered

    async def _deliver(self, plan: ReminderPlan, run: ReminderRun) -> None:
        try:
            await self._notifier.send_reminder(plan)
        except RecipientUnreachable as exc:
            # Retrying cannot help, so the account is retired and the slot marked
            # used — otherwise every tick would try the same dead chat forever.
            log.info("reminder_unreachable", user_id=plan.user.id, error=str(exc))
            await self._users.set_blocked(plan.user, blocked=True)
            await self._users.record_reminder(plan.user, kind=plan.kind, on=plan.local_day)
            run.unreachable.append(plan.user.id)
            return
        except Exception as exc:
            # Transient: no watermark, so the next tick picks this learner up again.
            log.warning("reminder_failed", user_id=plan.user.id, error=str(exc))
            run.failed.append(plan.user.id)
            return

        await self._users.record_reminder(plan.user, kind=plan.kind, on=plan.local_day)
        run.sent.append(plan)


def _evening_at(user: User, settings: ReminderSettings) -> time | None:
    if not user.evening_reminder:
        return None
    return user.evening_reminder_time or settings.default_streak_time


def build_reminder_service(
    session: AsyncSession,
    settings: Settings,
    notifier: Notifier,
) -> ReminderService:
    """Assemble the service for one tick's session.

    The same wiring serves the in-process loop and the ``cron`` entry point, so
    the two can never disagree about who is due.
    """
    return ReminderService(
        users=UserRepository(session),
        cards=CardRepository(session),
        reviews=ReviewRepository(session),
        streaks=StreakRepository(session),
        notifier=notifier,
        settings=settings,
    )
