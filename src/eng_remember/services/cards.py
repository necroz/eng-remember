"""Use cases around the vocabulary itself: adding, editing, counting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from eng_remember.domain.srs import CardSchedule, SrsScheduler
from eng_remember.errors import DuplicateCardError
from eng_remember.models.card import Card
from eng_remember.models.deck import Deck
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft, CardRepository
from eng_remember.repositories.decks import DeckRepository
from eng_remember.repositories.reviews import ReviewRepository
from eng_remember.utils.time import ensure_utc, local_day_start, next_local_day_start, utcnow

__all__ = ["CardService", "ImportOutcome", "UserStats"]


@dataclass(slots=True)
class ImportOutcome:
    """What happened when a batch of drafts was saved.

    Duplicates are reported rather than raised: a user pasting ten words should
    get the nine new ones and a note about the tenth, not an error.
    """

    added: list[Card] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    skipped_in_batch: list[str] = field(default_factory=list)

    @property
    def added_terms(self) -> list[str]:
        return [card.term for card in self.added]

    @property
    def total_skipped(self) -> int:
        return len(self.skipped_existing) + len(self.skipped_in_batch)


@dataclass(frozen=True, slots=True)
class UserStats:
    total_cards: int
    due_now: int
    new_cards: int
    reviews_today: int
    new_today: int


class CardService:
    def __init__(
        self,
        *,
        cards: CardRepository,
        decks: DeckRepository,
        reviews: ReviewRepository,
        scheduler: SrsScheduler,
        default_deck_name: str,
    ) -> None:
        self._cards = cards
        self._decks = decks
        self._reviews = reviews
        self._scheduler = scheduler
        self._default_deck_name = default_deck_name

    async def default_deck(self, user: User) -> Deck:
        return await self._decks.get_or_create_default(user.id, name=self._default_deck_name)

    async def resolve_deck(self, user: User, deck_id: int | None) -> Deck:
        if deck_id is None:
            return await self.default_deck(user)
        deck = await self._decks.get(deck_id, user_id=user.id)
        if deck is None:
            return await self.default_deck(user)
        return deck

    async def add_draft(
        self,
        user: User,
        draft: CardDraft,
        *,
        deck: Deck | None = None,
        source: str = "manual",
        now: datetime | None = None,
    ) -> Card:
        """Save one card, or raise if the user already has this term."""
        cleaned = draft.normalized()
        if not cleaned.term or not cleaned.translation:
            raise DuplicateCardError(cleaned.term or "<empty>")

        target = deck or await self.default_deck(user)
        if await self._cards.find_by_term(user.id, cleaned.term) is not None:
            raise DuplicateCardError(cleaned.term)

        return await self._cards.add(
            user_id=user.id,
            deck_id=target.id,
            draft=cleaned,
            schedule=self._first_schedule(user, now=now),
            source=source,
        )

    async def add_drafts(
        self,
        user: User,
        drafts: list[CardDraft],
        *,
        deck: Deck | None = None,
        source: str = "llm",
        now: datetime | None = None,
    ) -> ImportOutcome:
        """Save a batch, reporting rather than raising on duplicates."""
        target = deck or await self.default_deck(user)
        outcome = ImportOutcome()
        seen: set[str] = set()

        known = await self._cards.existing_terms(user.id, [d.term for d in drafts])

        for draft in drafts:
            cleaned = draft.normalized()
            key = cleaned.term.lower()
            if not cleaned.term or not cleaned.translation:
                continue
            if key in seen:
                outcome.skipped_in_batch.append(cleaned.term)
                continue
            if key in known:
                outcome.skipped_existing.append(cleaned.term)
                continue

            seen.add(key)
            card = await self._cards.add(
                user_id=user.id,
                deck_id=target.id,
                draft=cleaned,
                schedule=self._first_schedule(user, now=now),
                source=source,
            )
            outcome.added.append(card)

        return outcome

    def _first_schedule(self, user: User, *, now: datetime | None = None) -> CardSchedule:
        """When a newly added word comes back for the first time.

        Not today: a word is added to be remembered later, and showing it again in
        the same session the learner typed it in is drilling, not spaced
        repetition. The learner's next midnight is the boundary, which is also
        where the daily budget of new cards resets.
        """
        moment = ensure_utc(now) or utcnow()
        return self._scheduler.fresh_schedule(
            moment, due_at=next_local_day_start(user.timezone, now=moment)
        )

    async def list_cards(
        self,
        user: User,
        *,
        deck_id: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Card]:
        return await self._cards.list_for_user(user.id, deck_id=deck_id, limit=limit, offset=offset)

    async def delete(self, user: User, card_id: int) -> Card | None:
        card = await self._cards.get(card_id, user_id=user.id)
        if card is None:
            return None
        await self._cards.delete(card)
        return card

    async def suspend(self, user: User, card_id: int, *, suspended: bool = True) -> Card | None:
        card = await self._cards.get(card_id, user_id=user.id)
        if card is None:
            return None
        await self._cards.set_suspended(card, suspended=suspended)
        return card

    async def stats(self, user: User, *, now: datetime | None = None) -> UserStats:
        moment = ensure_utc(now) or utcnow()
        day_start = local_day_start(user.timezone, now=moment)
        return UserStats(
            total_cards=await self._cards.count_for_user(user.id),
            due_now=await self._cards.count_due(user.id, now=moment),
            new_cards=await self._cards.count_new(user.id, now=moment),
            reviews_today=await self._reviews.count_since(user.id, since=day_start),
            new_today=await self._reviews.count_cards_introduced_since(user.id, since=day_start),
        )
