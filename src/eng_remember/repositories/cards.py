from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, Select, and_, case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.domain.srs import CardSchedule
from eng_remember.models.card import Card
from eng_remember.utils.time import ensure_utc

__all__ = ["CardDraft", "CardRepository", "QueueCounts"]


@dataclass(frozen=True, slots=True)
class QueueCounts:
    """How much work waits for one learner."""

    due: int = 0
    new: int = 0

    @property
    def total(self) -> int:
        return self.due + self.new


@dataclass(frozen=True, slots=True)
class CardDraft:
    """Content of a card, before it is persisted.

    Deliberately separate from both the LLM output schema and the ORM model: the
    parser produces it, the repository consumes it, and neither has to know about
    the other.
    """

    term: str
    translation: str
    transcription: str | None = None
    part_of_speech: str | None = None
    example: str | None = None
    notes: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    def normalized(self) -> CardDraft:
        """Trim whitespace; a trailing space must not create a duplicate card."""
        return CardDraft(
            term=self.term.strip(),
            translation=self.translation.strip(),
            transcription=_clean(self.transcription),
            part_of_speech=_clean(self.part_of_speech),
            example=_clean(self.example),
            notes=_clean(self.notes),
            tags=tuple(tag.strip() for tag in self.tags if tag.strip()),
        )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class CardRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        user_id: int,
        deck_id: int,
        draft: CardDraft,
        schedule: CardSchedule,
        source: str = "manual",
    ) -> Card:
        # Normalised here as well as in the service: this is the last point
        # before persistence, so no caller can store an untrimmed term and
        # quietly defeat duplicate detection.
        cleaned = draft.normalized()
        card = Card(
            user_id=user_id,
            deck_id=deck_id,
            term=cleaned.term,
            translation=cleaned.translation,
            transcription=cleaned.transcription,
            part_of_speech=cleaned.part_of_speech,
            example=cleaned.example,
            notes=cleaned.notes,
            tags=list(cleaned.tags),
            source=source,
            due_at=schedule.due_at,
        )
        card.apply_schedule(schedule)
        self._session.add(card)
        await self._session.flush()
        return card

    async def get(self, card_id: int, *, user_id: int) -> Card | None:
        stmt = select(Card).where(Card.id == card_id, Card.user_id == user_id)
        return await self._session.scalar(stmt)

    async def find_by_term(self, user_id: int, term: str) -> Card | None:
        stmt = select(Card).where(
            Card.user_id == user_id,
            func.lower(Card.term) == term.strip().lower(),
        )
        return await self._session.scalar(stmt)

    async def all_terms(self, user_id: int, *, limit: int = 5000) -> set[str]:
        """Every term the learner has, lower-cased.

        Used to filter suggestions. Personal collections are in the hundreds, so
        reading them once per discovery is cheaper than a per-suggestion query
        and gives exact de-duplication instead of approximate.
        """
        stmt = select(func.lower(Card.term)).where(Card.user_id == user_id).limit(limit)
        return set((await self._session.scalars(stmt)).all())

    async def existing_terms(self, user_id: int, terms: Sequence[str]) -> set[str]:
        """Lower-cased terms the user already has, for bulk duplicate filtering."""
        if not terms:
            return set()
        lowered = [term.strip().lower() for term in terms]
        stmt = select(func.lower(Card.term)).where(
            Card.user_id == user_id,
            func.lower(Card.term).in_(lowered),
        )
        return set((await self._session.scalars(stmt)).all())

    async def list_due(
        self,
        user_id: int,
        *,
        now: datetime,
        limit: int,
        deck_id: int | None = None,
    ) -> list[Card]:
        """Cards already in rotation whose interval has elapsed, earliest first."""
        stmt = self._queue_stmt(user_id, deck_id=deck_id).where(
            Card.reps > 0,
            Card.due_at <= now,
        )
        return list(
            (await self._session.scalars(stmt.order_by(Card.due_at, Card.id).limit(limit))).all()
        )

    async def list_new(
        self,
        user_id: int,
        *,
        now: datetime,
        limit: int,
        deck_id: int | None = None,
    ) -> list[Card]:
        """Cards that have never been answered and are not postponed.

        The ``due_at`` filter is what makes "later" work on a brand-new card:
        without it the card is postponed and handed straight back.
        """
        if limit <= 0:
            return []
        stmt = self._queue_stmt(user_id, deck_id=deck_id).where(
            Card.reps == 0,
            Card.due_at <= now,
        )
        return list((await self._session.scalars(stmt.order_by(Card.id).limit(limit))).all())

    async def queue_counts(
        self,
        user_ids: Sequence[int],
        *,
        now: datetime,
    ) -> dict[int, QueueCounts]:
        """Due and new counts for many learners in one query.

        The reminder tick examines a batch of users; asking per user would be one
        round trip per notification, which is exactly the shape that turns a
        periodic job into a problem at a few thousand accounts.
        """
        if not user_ids:
            return {}

        due_case = case((and_(Card.reps > 0, Card.due_at <= now), 1), else_=0)
        new_case = case((and_(Card.reps == 0, Card.due_at <= now), 1), else_=0)
        stmt = (
            select(Card.user_id, func.sum(due_case), func.sum(new_case))
            .where(
                Card.user_id.in_(list(user_ids)),
                Card.is_suspended.is_(False),
            )
            .group_by(Card.user_id)
        )
        rows = (await self._session.execute(stmt)).all()
        counted = {
            int(user_id): QueueCounts(due=int(due or 0), new=int(new or 0))
            for user_id, due, new in rows
        }
        return {user_id: counted.get(user_id, QueueCounts()) for user_id in user_ids}

    async def count_due(self, user_id: int, *, now: datetime, deck_id: int | None = None) -> int:
        stmt = select(func.count(Card.id)).where(
            Card.user_id == user_id,
            Card.is_suspended.is_(False),
            Card.reps > 0,
            Card.due_at <= now,
        )
        if deck_id is not None:
            stmt = stmt.where(Card.deck_id == deck_id)
        return int(await self._session.scalar(stmt) or 0)

    async def next_due_at(self, user_id: int, *, now: datetime) -> datetime | None:
        """When the earliest scheduled card comes back — for the empty-queue screen.

        Unseen cards count too: a word added today is scheduled for tomorrow, and
        that is exactly what a learner staring at an empty queue should be told.
        """
        stmt = select(func.min(Card.due_at)).where(
            Card.user_id == user_id,
            Card.is_suspended.is_(False),
            Card.due_at > now,
        )
        return ensure_utc(await self._session.scalar(stmt))

    async def count_new(self, user_id: int, *, now: datetime, deck_id: int | None = None) -> int:
        """New cards waiting *now* — postponed ones are not in the queue."""
        stmt = select(func.count(Card.id)).where(
            Card.user_id == user_id,
            Card.is_suspended.is_(False),
            Card.reps == 0,
            Card.due_at <= now,
        )
        if deck_id is not None:
            stmt = stmt.where(Card.deck_id == deck_id)
        return int(await self._session.scalar(stmt) or 0)

    async def count_for_user(self, user_id: int, *, deck_id: int | None = None) -> int:
        stmt = select(func.count(Card.id)).where(Card.user_id == user_id)
        if deck_id is not None:
            stmt = stmt.where(Card.deck_id == deck_id)
        return int(await self._session.scalar(stmt) or 0)

    async def count_by_deck(self, user_id: int) -> dict[int, int]:
        stmt = (
            select(Card.deck_id, func.count(Card.id))
            .where(Card.user_id == user_id)
            .group_by(Card.deck_id)
        )
        return dict((await self._session.execute(stmt)).all())

    async def list_for_user(
        self,
        user_id: int,
        *,
        deck_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Card]:
        stmt = select(Card).where(Card.user_id == user_id)
        if deck_id is not None:
            stmt = stmt.where(Card.deck_id == deck_id)
        stmt = stmt.order_by(Card.term).offset(offset).limit(limit)
        return list((await self._session.scalars(stmt)).all())

    async def update_schedule(self, card: Card, schedule: CardSchedule) -> None:
        card.apply_schedule(schedule)
        await self._session.flush()

    async def update_content(self, card: Card, draft: CardDraft) -> None:
        card.translation = draft.translation
        card.transcription = draft.transcription
        card.part_of_speech = draft.part_of_speech
        card.example = draft.example
        card.notes = draft.notes
        card.tags = list(draft.tags)
        await self._session.flush()

    async def set_suspended(self, card: Card, *, suspended: bool) -> None:
        card.is_suspended = suspended
        await self._session.flush()

    async def delete(self, card: Card) -> None:
        await self._session.delete(card)
        await self._session.flush()

    async def delete_all(self, user_id: int, *, deck_id: int | None = None) -> int:
        stmt = delete(Card).where(Card.user_id == user_id)
        if deck_id is not None:
            stmt = stmt.where(Card.deck_id == deck_id)
        result = await self._session.execute(stmt)
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    @staticmethod
    def _queue_stmt(user_id: int, *, deck_id: int | None) -> Select[Card]:
        stmt = select(Card).where(
            Card.user_id == user_id,
            Card.is_suspended.is_(False),
        )
        if deck_id is not None:
            stmt = stmt.where(Card.deck_id == deck_id)
        return stmt
