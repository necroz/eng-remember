"""The background half of card enrichment.

The review flow only queues a card id; this module owns everything after that —
its own database session (the request's is long gone), the model call, and the
promise that no failure ever escapes into the bot. Backfill is a chore, and a
chore that can crash the process is worse than no chore at all.
"""

from __future__ import annotations

import asyncio

import structlog

from eng_remember.config import Settings
from eng_remember.db.engine import SessionFactory, session_scope
from eng_remember.services.container import build_services
from eng_remember.services.enrichment import BackfillQueue
from eng_remember.services.llm.client import LlmTransport

__all__ = ["enrichment_worker"]

log = structlog.get_logger(__name__)

# How often the worker wakes to notice it should stop when the queue is idle.
POLL_SECONDS = 1.0


async def enrichment_worker(
    session_factory: SessionFactory,
    settings: Settings,
    stopping: asyncio.Event,
    *,
    queue: BackfillQueue,
    transport: LlmTransport | None = None,
) -> None:
    """Consume backfill requests until asked to stop.

    The queue and the transport are the process-wide ones from the entry point:
    the worker outlives requests, so it must neither build a connection of its
    own nor look for work in module state.
    """
    while not stopping.is_set():
        try:
            user_id, card_id = await asyncio.wait_for(queue.get(), timeout=POLL_SECONDS)
        except TimeoutError:
            continue
        except asyncio.CancelledError:
            raise
        await _enrich_card(
            session_factory, settings, user_id, card_id, transport=transport, queue=queue
        )


async def _enrich_card(
    session_factory: SessionFactory,
    settings: Settings,
    user_id: int,
    card_id: int,
    *,
    transport: LlmTransport | None = None,
    queue: BackfillQueue | None = None,
) -> bool:
    """Top up one card's examples in a session of its own.

    Returns whether anything was added. The card may have been answered,
    enriched or deleted while this task waited its turn, so everything is
    re-checked here against fresh state.
    """
    try:
        async with session_scope(session_factory) as session:
            services = build_services(session, settings, llm_transport=transport, backfill=queue)
            card = await services.cards.get(card_id, user_id=user_id)
            if card is None or not services.enrichment.needs_examples(card):
                return False
            added = await services.enrichment.enrich_examples(card=card, user_id=user_id)
            return bool(added)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("enrichment_failed", card_id=card_id, error=str(exc))
        return False
