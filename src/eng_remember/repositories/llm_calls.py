from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.models.llm_call import LlmCall

__all__ = ["LlmCallRecord", "LlmCallRepository"]


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    """One model call, as reported by the LLM client.

    The client knows nothing about the database, so it hands back this plain
    record; a service persists it. That keeps the network layer testable without
    a session and the audit table optional.
    """

    purpose: str
    provider_model: str
    success: bool
    user_id: int | None = None
    attempt: int = 1
    request_text: str | None = None
    response_text: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None
    error: str | None = None


class LlmCallRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, record: LlmCallRecord) -> LlmCall:
        call = LlmCall(
            user_id=record.user_id,
            purpose=record.purpose,
            provider_model=record.provider_model,
            attempt=record.attempt,
            request_text=record.request_text,
            response_text=record.response_text,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            total_tokens=record.total_tokens,
            latency_ms=record.latency_ms,
            success=record.success,
            error=record.error,
        )
        self._session.add(call)
        await self._session.flush()
        return call

    async def usage_since(
        self,
        user_id: int,
        *,
        since: datetime,
        background_purpose: str | None = None,
    ) -> dict[str, int]:
        """Calls, tokens and background calls for one learner in the window.

        ``background_calls`` is zero unless a purpose is supplied; the quota
        gate uses it to keep example generation from consuming the interactive
        call budget.
        """
        background = (
            func.coalesce(
                func.sum(case((LlmCall.purpose == background_purpose, 1), else_=0)),
                0,
            )
            if background_purpose is not None
            else literal(0)
        )
        stmt = select(
            func.count(LlmCall.id),
            func.coalesce(func.sum(LlmCall.total_tokens), 0),
            background,
        ).where(LlmCall.user_id == user_id, LlmCall.created_at >= since)
        calls, tokens, background_calls = (await self._session.execute(stmt)).one()
        return {
            "calls": int(calls or 0),
            "tokens": int(tokens or 0),
            "background_calls": int(background_calls or 0),
        }

    async def global_usage_since(self, *, since: datetime) -> dict[str, int]:
        """Calls and tokens across every learner in the window."""
        stmt = select(
            func.count(LlmCall.id),
            func.coalesce(func.sum(LlmCall.total_tokens), 0),
        ).where(LlmCall.created_at >= since)
        calls, tokens = (await self._session.execute(stmt)).one()
        return {"calls": int(calls or 0), "tokens": int(tokens or 0)}
