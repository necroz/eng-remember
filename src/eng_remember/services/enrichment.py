"""Keeping cards supplied with example sentences.

A card earns its keep by being shown in context, never twice in the same
sentence: rotation only works when there is something to rotate through. New
LLM-built cards arrive with several examples; older cards are topped up in the
background, one at a time, through a bounded queue the review flow feeds.

The queue is deliberately droppable. Backfill is a chore, not a promise — if
the bot is busy, the next repetition of the same card asks again.
"""

from __future__ import annotations

import asyncio

import structlog

from eng_remember.config import Settings
from eng_remember.domain.examples import merge_examples
from eng_remember.errors import LlmResponseError
from eng_remember.models.card import Card
from eng_remember.repositories.cards import CardRepository
from eng_remember.services.llm.enricher import CardEnricher

__all__ = ["BACKFILL_QUEUE_SIZE", "BackfillQueue", "EnrichmentService"]

log = structlog.get_logger(__name__)

BACKFILL_QUEUE_SIZE = 100


class BackfillQueue:
    """The bounded hand-off between review requests and the backfill worker.

    Explicit ownership: the entry point creates one instance and gives the same
    object to every request's services and to the worker. Nothing is shared
    through module state, so two bots in one process — tests, mostly — cannot
    fill or drain each other's queue.
    """

    def __init__(self, *, maxsize: int = BACKFILL_QUEUE_SIZE) -> None:
        self._items: asyncio.Queue[tuple[int, int]] = asyncio.Queue(maxsize=maxsize)

    def offer(self, user_id: int, card_id: int) -> bool:
        """Queue one card without waiting; false when the queue is full."""
        try:
            self._items.put_nowait((user_id, card_id))
        except asyncio.QueueFull:
            log.warning("enrichment_queue_full", user_id=user_id, card_id=card_id)
            return False
        return True

    async def get(self) -> tuple[int, int]:
        return await self._items.get()

    def __len__(self) -> int:
        return self._items.qsize()


class EnrichmentService:
    def __init__(
        self,
        *,
        cards: CardRepository,
        enricher: CardEnricher,
        settings: Settings,
        queue: BackfillQueue | None = None,
    ) -> None:
        self._cards = cards
        self._enricher = enricher
        self._settings = settings
        # A service built without a queue gets one of its own: that keeps tests
        # and one-off callers self-contained, while the bot passes the process
        # queue in so the worker sees what the handlers enqueue.
        self._queue = queue if queue is not None else BackfillQueue()

    @property
    def queue(self) -> BackfillQueue:
        return self._queue

    @property
    def target(self) -> int:
        return self._settings.review.examples_per_card

    def enqueue(self, *, user_id: int, card_id: int) -> bool:
        """Ask the background worker to top up one card's examples."""
        return self._queue.offer(user_id, card_id)

    def needs_examples(self, card: Card) -> bool:
        return self._enricher.available and len(self._existing(card)) < self.target

    async def enrich_examples(self, *, card: Card, user_id: int) -> list[str]:
        """Top the card up to the target. Returns only the new sentences."""
        existing = self._existing(card)
        missing = self.target - len(existing)
        if missing <= 0:
            return []

        generated = await self._enricher.examples(
            term=card.term,
            translation=card.translation,
            count=missing,
            existing=existing,
            user_id=user_id,
            language=card.language,
        )
        if not generated:
            return []

        # The model call takes time: the learner may have edited or deleted the
        # card meanwhile. Merge into what is stored *now* rather than into the
        # snapshot the call started from, so their sentence is never overwritten.
        fresh = await self._cards.refresh(card)
        if fresh is None:
            return []
        current = self._existing(fresh)
        merged = merge_examples(fresh.example, [*fresh.examples, *generated], limit=self.target)
        known = {item.casefold() for item in current}
        added = [item for item in merged if item.casefold() not in known]
        await self._cards.update_examples(fresh, merged)
        return added

    async def add_example(self, *, card: Card, user_id: int) -> str:
        """One more sentence for a card the learner asked to refresh.

        Unlike the background top-up this one keeps no ceiling: the learner
        pressed the button on purpose, and the list is what rotation walks.
        """
        existing = self._existing(card)
        generated = await self._enricher.examples(
            term=card.term,
            translation=card.translation,
            count=1,
            existing=existing,
            user_id=user_id,
            language=card.language,
        )
        if not generated:
            raise LlmResponseError("the model returned no examples")

        sentence = generated[0]
        merged = merge_examples(card.example, [*card.examples, sentence])
        await self._cards.update_examples(card, merged)
        return sentence

    async def add_mnemonic(self, *, card: Card, user_id: int) -> str:
        """A memory hook, stored between the card's usage notes."""
        mnemonic = await self._enricher.mnemonic(
            term=card.term,
            translation=card.translation,
            user_id=user_id,
            language=card.language,
        )
        notes = f"{card.notes} · {mnemonic}" if card.notes else mnemonic
        await self._cards.update_notes(card, notes)
        return mnemonic

    def _existing(self, card: Card) -> list[str]:
        return merge_examples(card.example, card.examples)
