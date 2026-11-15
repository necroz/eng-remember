from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
            # Streak reads far outnumber writes; only a missing row costs one.
            return existing

        # ``user_id`` is unique, so the insert either creates the starting row or
        # loses to a concurrent first answer. A single statement keeps that race
        # from ever surfacing as an IntegrityError.
        dialect_insert = (
            sqlite_insert
            if self._session.get_bind().dialect.name == "sqlite"
            else postgresql_insert
        )
        statement = (
            dialect_insert(Streak)
            .values(
                user_id=user_id,
                current=initial.current,
                longest=initial.longest,
                freezes=initial.freezes,
                missed_days=initial.missed_days,
                accounted_on=initial.accounted_on,
            )
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        await self._session.execute(statement)

        streak = await self.get(user_id)
        if streak is None:  # pragma: no cover — the row was just inserted or read back
            raise RuntimeError(f"streak for user {user_id} vanished after upsert")
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
