"""Assembling the LLM client with its audit sink.

Separate from the clients so the parser and the explorer share one wiring path:
two independent places building an ``OpenAiCompatibleClient`` would eventually
differ in whether they record calls.
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.config import Settings
from eng_remember.repositories.llm_calls import LlmCallRecord, LlmCallRepository
from eng_remember.services.llm.client import JsonChatClient, OpenAiCompatibleClient
from eng_remember.services.llm.quota import QuotaLimitedClient

__all__ = ["build_llm_client"]


def build_llm_client(session: AsyncSession, settings: Settings) -> JsonChatClient | None:
    """A client bound to this request's session, or ``None`` when unconfigured.

    Returning ``None`` rather than a disabled client keeps the call sites honest:
    every caller has to decide what it does without a model, and that decision is
    visible in its code instead of being buried in a stub.

    The client is wrapped in the per-learner quota gate here rather than at the
    call sites, so the parser and the explorer cannot drift apart in whether
    they respect the budget — and neither can a future third caller.
    """
    if not settings.llm.is_configured:
        return None

    repository = LlmCallRepository(session)
    log_payloads = settings.log_llm_payloads

    async def sink(record: LlmCallRecord) -> None:
        # Prompt and answer carry whatever the user typed, so they are stored
        # only on request; metrics are always kept.
        if not log_payloads:
            record = replace(record, request_text=None, response_text=None)
        await repository.add(record)

    client = OpenAiCompatibleClient(settings.llm, sink=sink)
    return QuotaLimitedClient(client, calls=repository, settings=settings.llm)
