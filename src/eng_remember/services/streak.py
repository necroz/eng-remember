"""Use cases around the daily streak.

Wraps the pure rules in :mod:`eng_remember.domain.gamification` with the two
things they deliberately do not know about: where the state is stored, and what
"today" means for this particular learner.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime

from eng_remember.config import StreakSettings
from eng_remember.domain.gamification import (
    StreakState,
    StreakTransition,
    record_activity,
    roll_forward,
)
from eng_remember.models.user import User
from eng_remember.repositories.streaks import StreakRepository
from eng_remember.utils.time import local_date

__all__ = ["StreakService"]

# Both domain rules share this shape: settle a state as of a given local day.
StreakRule = Callable[[StreakState, date, StreakSettings], StreakTransition]


class StreakService:
    def __init__(self, *, streaks: StreakRepository, settings: StreakSettings) -> None:
        self._streaks = streaks
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    async def current(self, user: User, *, now: datetime | None = None) -> StreakTransition:
        """The streak as of the learner's today, without counting today.

        Opening ``/stats`` after a gap must show the streak as already broken —
        otherwise the number only corrects itself after the next answer, which
        reads like the bot lost track.
        """
        return await self._apply(user, roll_forward, now=now)

    async def record(self, user: User, *, now: datetime | None = None) -> StreakTransition:
        """Count today, absorbing any gap since the last answer."""
        return await self._apply(user, record_activity, now=now)

    async def _apply(
        self,
        user: User,
        rule: StreakRule,
        *,
        now: datetime | None,
    ) -> StreakTransition:
        if not self.enabled:
            # Switched off: answer with an empty state and write nothing, so
            # disabling the feature leaves no rows behind.
            return StreakTransition(state=StreakState())

        streak = await self._streaks.get_or_create(
            user.id, initial=StreakState.fresh(self._settings)
        )
        transition = rule(streak.state, local_date(user.timezone, now=now), self._settings)
        if transition.state != streak.state:
            await self._streaks.save(streak, transition.state)
        return transition
