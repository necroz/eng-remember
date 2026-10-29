from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    CursorResult,
    Select,
    and_,
    case,
    delete,
    exists,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from eng_remember.domain.enums import CardDirection
from eng_remember.domain.examples import merge_examples
from eng_remember.domain.languages import DEFAULT_LANGUAGE, resolve_target_language
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
    examples: tuple[str, ...] = field(default_factory=tuple)
    notes: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    cefr: str | None = None
    frequency: str | None = None
    language: str = DEFAULT_LANGUAGE

    def normalized(self) -> CardDraft:
        """Trim whitespace; a trailing space must not create a duplicate card.

        The example list keeps its invariant here: the main example is the
        first element, so rotation always starts with the sentence the learner
        may have pinned themselves.
        """
        examples = tuple(merge_examples(self.example, self.examples))
        return CardDraft(
            term=self.term.strip(),
            translation=self.translation.strip(),
            transcription=_clean(self.transcription),
            part_of_speech=_clean(self.part_of_speech),
            example=examples[0] if examples else None,
            examples=examples,
            notes=_clean(self.notes),
            tags=tuple(tag.strip() for tag in self.tags if tag.strip()),
            cefr=_clean(self.cefr),
            frequency=_clean(self.frequency),
            language=resolve_target_language(self.language),
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
        direction: CardDirection = CardDirection.FORWARD,
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
            language=cleaned.language,
            transcription=cleaned.transcription,
            part_of_speech=cleaned.part_of_speech,
            example=cleaned.example,
            examples=list(cleaned.examples),
            notes=cleaned.notes,
            tags=list(cleaned.tags),
            cefr=cleaned.cefr,
            frequency=cleaned.frequency,
            source=source,
            direction=direction,
            due_at=schedule.due_at,
        )
        card.apply_schedule(schedule)
        self._session.add(card)
        await self._session.flush()
        return card

    async def get(self, card_id: int, *, user_id: int) -> Card | None:
        stmt = select(Card).where(Card.id == card_id, Card.user_id == user_id)
        return await self._session.scalar(stmt)

    async def find_by_term(
        self, user_id: int, term: str, language: str = DEFAULT_LANGUAGE
    ) -> Card | None:
        stmt = select(Card).where(
            Card.user_id == user_id,
            func.lower(Card.term) == term.strip().lower(),
            Card.language == resolve_target_language(language),
        )
        return await self._session.scalar(stmt)

    async def find_twin(self, card: Card) -> Card | None:
        """The same word in the other direction, if it exists."""
        stmt = select(Card).where(
            Card.user_id == card.user_id,
            Card.deck_id == card.deck_id,
            Card.term == card.term,
            Card.language == card.language,
            Card.direction == card.direction.flipped,
        )
        return await self._session.scalar(stmt)

    async def all_terms(
        self, user_id: int, *, limit: int = 5000, language: str | None = None
    ) -> set[str]:
        """Every term the learner has, case-folded.

        Used to filter suggestions. Personal collections are in the hundreds, so
        reading them once per discovery is cheaper than a per-suggestion query
        and gives exact de-duplication instead of approximate. A language limits
        the set to the words that would collide with a new card.
        """
        stmt = select(func.lower(Card.term)).where(Card.user_id == user_id)
        if language is not None:
            stmt = stmt.where(Card.language == resolve_target_language(language))
        stmt = stmt.limit(limit)
        return {term.casefold() for term in (await self._session.scalars(stmt)).all()}

    async def existing_terms(
        self,
        user_id: int,
        terms: Sequence[str],
        language: str | None = None,
    ) -> set[str]:
        """Case-folded terms the user already has, for bulk duplicate filtering."""
        if not terms:
            return set()
        lowered = [term.strip().lower() for term in terms]
        stmt = select(func.lower(Card.term)).where(
            Card.user_id == user_id,
            func.lower(Card.term).in_(lowered),
        )
        if language is not None:
            stmt = stmt.where(Card.language == resolve_target_language(language))
        return {term.casefold() for term in (await self._session.scalars(stmt)).all()}

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
        bury_reviewed_since: datetime | None = None,
    ) -> list[Card]:
        """Cards that have never been answered and are not postponed.

        The ``due_at`` filter is what makes "later" work on a brand-new card:
        without it the card is postponed and handed straight back.

        ``bury_reviewed_since`` drops a new card whose sibling was answered
        after that moment: the second direction of a word practised today is
        not new material. The exclusion lives in SQL so a run of buried twins
        cannot exhaust a small candidate window.
        """
        if limit <= 0:
            return []
        stmt = self._queue_stmt(user_id, deck_id=deck_id).where(
            Card.reps == 0,
            Card.due_at <= now,
        )
        if bury_reviewed_since is not None:
            sibling = aliased(Card)
            stmt = stmt.where(
                ~exists().where(
                    sibling.user_id == Card.user_id,
                    sibling.id != Card.id,
                    func.lower(sibling.term) == func.lower(Card.term),
                    sibling.language == Card.language,
                    sibling.reps > 0,
                    sibling.last_review_at.is_not(None),
                    sibling.last_review_at >= bury_reviewed_since,
                )
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
        new_case = case(
            (
                and_(
                    Card.reps == 0,
                    Card.due_at <= now,
                    Card.direction == CardDirection.FORWARD,
                ),
                1,
            ),
            else_=0,
        )
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
        """New words waiting *now* — postponed ones are not in the queue.

        Counted per forward card: bury introduces one direction per word a day,
        so advertising both twins would promise twice the work a session can
        serve.
        """
        stmt = select(func.count(Card.id)).where(
            Card.user_id == user_id,
            Card.is_suspended.is_(False),
            Card.reps == 0,
            Card.due_at <= now,
            Card.direction == CardDirection.FORWARD,
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
        direction: CardDirection | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Card]:
        stmt = select(Card).where(Card.user_id == user_id)
        if deck_id is not None:
            stmt = stmt.where(Card.deck_id == deck_id)
        if direction is not None:
            stmt = stmt.where(Card.direction == direction)
        stmt = stmt.order_by(Card.term).offset(offset).limit(limit)
        return list((await self._session.scalars(stmt)).all())

    async def list_seen(
        self,
        user_id: int,
        *,
        limit: int = 500,
        direction: CardDirection | None = CardDirection.FORWARD,
    ) -> list[Card]:
        """Cards already in rotation. Order is deliberately whatever the
        database returns; the practice service does its own sampling."""
        stmt = select(Card).where(
            Card.user_id == user_id,
            Card.is_suspended.is_(False),
            Card.reps > 0,
        )
        if direction is not None:
            stmt = stmt.where(Card.direction == direction)
        return list((await self._session.scalars(stmt.limit(limit))).all())

    async def update_schedule(self, card: Card, schedule: CardSchedule) -> None:
        card.apply_schedule(schedule)
        await self._session.flush()

    async def update_content(self, card: Card, draft: CardDraft) -> None:
        card.translation = draft.translation
        card.transcription = draft.transcription
        card.part_of_speech = draft.part_of_speech
        card.example = draft.example
        card.examples = list(draft.examples)
        card.notes = draft.notes
        card.tags = list(draft.tags)
        card.cefr = draft.cefr
        card.frequency = draft.frequency
        await self._session.flush()

    async def update_examples(self, card: Card, examples: list[str]) -> None:
        card.examples = list(examples)
        card.example = examples[0] if examples else None
        await self._session.flush()

    async def update_notes(self, card: Card, notes: str | None) -> None:
        card.notes = notes
        await self._session.flush()

    async def list_suspended(self, user_id: int, *, limit: int = 20) -> list[Card]:
        """Paused words once each — the forward card stands for its pair."""
        stmt = (
            select(Card)
            .where(
                Card.user_id == user_id,
                Card.is_suspended.is_(True),
                Card.direction == CardDirection.FORWARD,
            )
            .order_by(Card.term)
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())

    async def count_suspended(self, user_id: int) -> int:
        stmt = select(func.count(Card.id)).where(
            Card.user_id == user_id,
            Card.is_suspended.is_(True),
            Card.direction == CardDirection.FORWARD,
        )
        return int(await self._session.scalar(stmt) or 0)

    async def count_added_since(
        self,
        user_id: int,
        *,
        since: datetime,
        until: datetime | None = None,
        direction: CardDirection | None = None,
    ) -> int:
        stmt = select(func.count(Card.id)).where(
            Card.user_id == user_id,
            Card.created_at >= since,
        )
        if until is not None:
            stmt = stmt.where(Card.created_at < until)
        if direction is not None:
            stmt = stmt.where(Card.direction == direction)
        return int(await self._session.scalar(stmt) or 0)

    async def weakest(self, user_id: int, *, limit: int = 3) -> list[Card]:
        """The words that failed most, for the weekly digest.

        Forward cards only: a word is one entry, not two, however often each
        of its sides has been failed.
        """
        stmt = (
            select(Card)
            .where(
                Card.user_id == user_id,
                Card.again_count > 0,
                Card.direction == CardDirection.FORWARD,
            )
            .order_by(Card.again_count.desc(), Card.term)
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())

    async def count_due_within(self, user_id: int, *, now: datetime, until: datetime) -> int:
        """Cards already in rotation that come due in a window after ``now``."""
        stmt = select(func.count(Card.id)).where(
            Card.user_id == user_id,
            Card.is_suspended.is_(False),
            Card.reps > 0,
            Card.due_at > now,
            Card.due_at <= until,
        )
        return int(await self._session.scalar(stmt) or 0)

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
