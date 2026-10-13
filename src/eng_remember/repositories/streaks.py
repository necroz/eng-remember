from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.domain.gamification import StreakState
from eng_remember.models.streak import Streak

__all__ = ["StreakRepository"]


class StreakRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> Streak | None:
        stmt = select(Streak).where(Streak.user_id == user_id)
        return await self._session.scalar(stmt)

    async def get_or_create(self, user_id: int, *, initial: StreakState) -> Streak:
        existing = await self.get(user_id)
        if existing is not None:
            return existing

        streak = Streak(user_id=user_id)
        streak.apply(initial)
        self._session.add(streak)
        await self._session.flush()
        return streak

    async def by_user_ids(self, user_ids: Sequence[int]) -> dict[int, StreakState]:
        """Streak states for a batch, with a default for learners who have none."""
        if not user_ids:
            return {}
        stmt = select(Streak).where(Streak.user_id.in_(list(user_ids)))
        rows = (await self._session.scalars(stmt)).all()
        found = {streak.user_id: streak.state for streak in rows}
        return {user_id: found.get(user_id, StreakState()) for user_id in user_ids}

    async def save(self, streak: Streak, state: StreakState) -> Streak:
        streak.apply(state)
        await self._session.flush()
        return streak
