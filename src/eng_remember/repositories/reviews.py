from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.models.review import Review

__all__ = ["ReviewRepository"]


class ReviewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, review: Review) -> Review:
        self._session.add(review)
        await self._session.flush()
        return review

    async def list_for_card(self, card_id: int, *, limit: int = 50) -> list[Review]:
        stmt = (
            select(Review)
            .where(Review.card_id == card_id)
            .order_by(Review.reviewed_at.desc())
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())

    async def count_since(self, user_id: int, *, since: datetime) -> int:
        stmt = select(func.count(Review.id)).where(
            Review.user_id == user_id,
            Review.reviewed_at >= since,
        )
        return int(await self._session.scalar(stmt) or 0)

    async def counts_since(
        self,
        user_ids: Sequence[int],
        *,
        since: datetime,
    ) -> dict[int, int]:
        """Answers per learner since one shared moment.

        Callers group users by their local midnight first: a handful of distinct
        boundaries covers a whole batch, which keeps the reminder tick to a few
        queries instead of one per learner.
        """
        if not user_ids:
            return {}
        stmt = (
            select(Review.user_id, func.count(Review.id))
            .where(
                Review.user_id.in_(list(user_ids)),
                Review.reviewed_at >= since,
            )
            .group_by(Review.user_id)
        )
        rows = (await self._session.execute(stmt)).all()
        counted = {int(user_id): int(count) for user_id, count in rows}
        return {user_id: counted.get(user_id, 0) for user_id in user_ids}

    async def count_cards_introduced_since(self, user_id: int, *, since: datetime) -> int:
        """How many cards got their very first answer after ``since``.

        Driven by the review log rather than by ``cards.reps``, because a card
        answered twice today must still count as one introduction.
        """
        first_seen = (
            select(Review.card_id)
            .where(Review.user_id == user_id)
            .group_by(Review.card_id)
            .having(func.min(Review.reviewed_at) >= since)
            .subquery()
        )
        stmt = select(func.count()).select_from(first_seen)
        return int(await self._session.scalar(stmt) or 0)

    async def daily_counts(self, user_id: int, *, since: datetime) -> list[tuple[date, int]]:
        """Answers per day, for the statistics screen."""
        day = func.date(Review.reviewed_at)
        stmt = (
            select(day, func.count(Review.id))
            .where(Review.user_id == user_id, Review.reviewed_at >= since)
            .group_by(day)
            .order_by(day)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], int(row[1])) for row in rows]
