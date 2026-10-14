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

__all__ = ["BACKFILL_QUEUE_SIZE", "EnrichmentService", "enqueue_examples"]

log = structlog.get_logger(__name__)

BACKFILL_QUEUE_SIZE = 100

_QUEUE: asyncio.Queue[tuple[int, int]] = asyncio.Queue(maxsize=BACKFILL_QUEUE_SIZE)


def enqueue_examples(user_id: int, card_id: int) -> bool:
    """Ask the background worker to top up one card's examples."""
    try:
        _QUEUE.put_nowait((user_id, card_id))
    except asyncio.QueueFull:
        log.warning("enrichment_queue_full", user_id=user_id, card_id=card_id)
        return False
    return True


class EnrichmentService:
    def __init__(
        self,
        *,
        cards: CardRepository,
        enricher: CardEnricher,
        settings: Settings,
    ) -> None:
        self._cards = cards
        self._enricher = enricher
        self._settings = settings

    @property
    def target(self) -> int:
        return self._settings.review.examples_per_card

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
        )
        merged = merge_examples(card.example, [*card.examples, *generated], limit=self.target)
        added = [item for item in merged if item not in existing]
        await self._cards.update_examples(card, merged)
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
        )
        notes = f"{card.notes} · {mnemonic}" if card.notes else mnemonic
        await self._cards.update_notes(card, notes)
        return mnemonic

    def _existing(self, card: Card) -> list[str]:
        return merge_examples(card.example, card.examples)
