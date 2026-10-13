"""Per-learner limits on how much model usage one account may cause.

The gate sits in front of the client rather than at the call sites: the parser
and the explorer both go through this wrapper, so a future feature that asks
the model for something inherits the limit instead of having to remember it.

Usage is read from the audit table, which the client's own sink writes as calls
happen — the numbers the operator sees in ``llm_calls`` are exactly the numbers
the gate enforces. Windows are rolling: "60 per hour" means any 60 minutes, not
a clock hour.
"""

from __future__ import annotations

from datetime import timedelta
from typing import NoReturn, TypeVar

import structlog
from pydantic import BaseModel

from eng_remember.config import LlmSettings
from eng_remember.errors import LlmQuotaExceededError
from eng_remember.repositories.llm_calls import LlmCallRepository
from eng_remember.services.llm.client import JsonChatClient
from eng_remember.utils.time import utcnow

__all__ = ["QuotaLimitedClient"]

log = structlog.get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

CALL_WINDOW = timedelta(hours=1)
TOKEN_WINDOW = timedelta(hours=24)


class QuotaLimitedClient:
    """A client that refuses to call the model once the learner is over budget."""

    def __init__(
        self,
        inner: JsonChatClient,
        *,
        calls: LlmCallRepository,
        settings: LlmSettings,
    ) -> None:
        self._inner = inner
        self._calls = calls
        self._settings = settings

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    @property
    def available(self) -> bool:
        return self._inner.available

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: type[ModelT],
        user_id: int | None = None,
    ) -> ModelT:
        if user_id is not None:
            await self._enforce(user_id)
        return await self._inner.complete_json(
            purpose=purpose,
            system=system,
            user=user,
            schema=schema,
            user_id=user_id,
        )

    async def _enforce(self, user_id: int) -> None:
        now = utcnow()
        if self._settings.max_calls_per_hour:
            usage = await self._calls.usage_since(user_id, since=now - CALL_WINDOW)
            if usage["calls"] >= self._settings.max_calls_per_hour:
                self._refuse(
                    user_id,
                    "calls_per_hour",
                    usage["calls"],
                    self._settings.max_calls_per_hour,
                )
        if self._settings.max_tokens_per_day:
            usage = await self._calls.usage_since(user_id, since=now - TOKEN_WINDOW)
            if usage["tokens"] >= self._settings.max_tokens_per_day:
                self._refuse(
                    user_id,
                    "tokens_per_day",
                    usage["tokens"],
                    self._settings.max_tokens_per_day,
                )

    @staticmethod
    def _refuse(user_id: int, window: str, used: int, limit: int) -> NoReturn:
        # A stable event name: this is the anomaly an operator wants an alert on.
        log.warning("llm_quota_exceeded", user_id=user_id, window=window, used=used, limit=limit)
        raise LlmQuotaExceededError(f"{window} limit reached: {used} used, limit {limit}")
