"""Per-learner limits on how much model usage one account may cause.

The gate sits in front of the client rather than at the call sites: the parser
and the explorer both go through this wrapper, so a future feature that asks
the model for something inherits the limit instead of having to remember it.

Usage is read from the audit table, which the client's own sink writes as calls
happen — the numbers the operator sees in ``llm_calls`` are exactly the numbers
the gate enforces. Both limits use a rolling 24-hour window: "60 calls per
day" means any 24 hours, not a calendar day.

The gate is deliberately process-local. It serializes one learner's model calls
through :mod:`eng_remember.utils.user_locks`; bounding total update concurrency
happens in the bot layer. With several replicas the same learner could still get
one quota check per replica. A shared reservation would be the next step if the
bot is scaled horizontally.
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
from eng_remember.utils.user_locks import UserLockRegistry, user_locks

__all__ = ["QuotaLimitedClient"]

log = structlog.get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

WINDOW = timedelta(hours=24)


class QuotaLimitedClient:
    """A client that refuses to call the model once the learner is over budget."""

    def __init__(
        self,
        inner: JsonChatClient,
        *,
        calls: LlmCallRepository,
        settings: LlmSettings,
        locks: UserLockRegistry | None = None,
    ) -> None:
        self._inner = inner
        self._calls = calls
        self._settings = settings
        self._locks = locks or user_locks

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
        if user_id is None:
            return await self._inner.complete_json(
                purpose=purpose,
                system=system,
                user=user,
                schema=schema,
                user_id=user_id,
            )

        # One learner, one model call at a time. The lock prevents parallel
        # calls; the quota itself reads committed audit rows, so a burst can
        # still overrun the soft cap slightly until the first transactions
        # commit. A hard cap needs an atomic reservation (see security.md).
        async with self._locks.hold(user_id):
            await self._enforce(user_id)
            return await self._inner.complete_json(
                purpose=purpose,
                system=system,
                user=user,
                schema=schema,
                user_id=user_id,
            )

    async def _enforce(self, user_id: int) -> None:
        if not self._settings.max_calls_per_day and not self._settings.max_tokens_per_day:
            return

        # One query covers both limits: they share the same rolling window.
        usage = await self._calls.usage_since(user_id, since=utcnow() - WINDOW)

        if self._settings.max_calls_per_day and usage["calls"] >= self._settings.max_calls_per_day:
            self._refuse(
                user_id,
                "calls_per_day",
                usage["calls"],
                self._settings.max_calls_per_day,
            )
        if (
            self._settings.max_tokens_per_day
            and usage["tokens"] >= self._settings.max_tokens_per_day
        ):
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
