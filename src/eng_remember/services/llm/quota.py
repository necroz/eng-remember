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

Some calls have no learner at all (``user_id=None``): there is no account to
lock and no per-learner quota to apply, so the gate does neither. The bot-wide
budget still applies — it protects the operator's spend rather than one account
— so it is checked for every call, with or without a learner. Its usage is read
from the audit table at most once per :data:`GLOBAL_USAGE_TTL_SECONDS` per
process: the total only moves as calls complete, and a full-table SUM in front
of every model call would make the gate the bottleneck it exists to prevent.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import timedelta
from typing import NoReturn, TypeVar

import structlog
from pydantic import BaseModel

from eng_remember.config import LlmSettings
from eng_remember.errors import LlmGlobalBudgetExceededError, LlmQuotaExceededError
from eng_remember.repositories.llm_calls import LlmCallRepository
from eng_remember.services.llm.budget import GlobalBudgetMonitor, global_budget
from eng_remember.services.llm.client import JsonChatClient, complete_json_with_model
from eng_remember.utils.time import utcnow
from eng_remember.utils.user_locks import UserLockRegistry, user_locks

__all__ = [
    "GLOBAL_USAGE_TTL_SECONDS",
    "GlobalUsageCache",
    "QuotaLimitedClient",
    "global_usage_cache",
]

log = structlog.get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

WINDOW = timedelta(hours=24)
# Background example generation gets its own call bucket. Keep this string in
# sync with services/llm/enricher.py:PURPOSE_EXAMPLES.
EXAMPLE_PURPOSE = "card_examples"

# Reads of the bot-wide total are reused for this long. A failed call or a burst
# can then overshoot the budget for up to this window, which is the price of not
# running an aggregate query in front of every single model call.
GLOBAL_USAGE_TTL_SECONDS = 30.0


class GlobalUsageCache:
    """The bot-wide token total, fetched from the audit table at most once per TTL.

    The quota wrapper is built per request because it is bound to that request's
    session, so a per-instance cache would be empty on every request and the SUM
    would run every time. The cache therefore lives at module level and is shared
    by every wrapper in the process (each replica has its own), like the budget
    monitor it feeds.

    The lock is what makes the TTL a bound on queries rather than a bound on
    misses: a burst of handlers that all see the cold cache queues on it, and
    only the first one queries. ``asyncio.Lock`` binds to the loop that first
    uses it, and tests run every case in a fresh loop, so the cache hands out one
    lock per loop instead of holding a single module-level one.
    """

    def __init__(self, ttl: float = GLOBAL_USAGE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._tokens: int | None = None
        self._read_at = 0.0
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def read(self, now: float) -> int | None:
        """The cached total, or ``None`` when it is missing or older than the TTL."""
        if self._tokens is None or now - self._read_at >= self._ttl:
            return None
        return self._tokens

    def remember(self, tokens: int, now: float) -> None:
        self._tokens = tokens
        self._read_at = now

    def clear(self) -> None:
        """Forget the cached total."""
        self._tokens = None
        self._read_at = 0.0

    def lock(self) -> asyncio.Lock:
        """The single-flight lock for the running event loop."""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock


global_usage_cache = GlobalUsageCache()


class QuotaLimitedClient:
    """A client that refuses to call the model once the learner is over budget."""

    def __init__(
        self,
        inner: JsonChatClient,
        *,
        calls: LlmCallRepository,
        settings: LlmSettings,
        locks: UserLockRegistry | None = None,
        budget: GlobalBudgetMonitor | None = None,
        usage_cache: GlobalUsageCache | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._inner = inner
        self._calls = calls
        self._settings = settings
        self._locks = locks or user_locks
        self._budget = budget or global_budget
        self._usage_cache = usage_cache or global_usage_cache
        self._clock = clock or time.monotonic

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
        result, _ = await self.complete_json_with_model(
            purpose=purpose, system=system, user=user, schema=schema, user_id=user_id
        )
        return result

    async def complete_json_with_model(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: type[ModelT],
        user_id: int | None = None,
    ) -> tuple[ModelT, str]:
        async def run() -> tuple[ModelT, str]:
            await self._enforce(user_id, purpose)
            return await complete_json_with_model(
                self._inner,
                purpose=purpose,
                system=system,
                user=user,
                schema=schema,
                user_id=user_id,
            )

        # Without a learner, only the bot-wide budget applies.
        if user_id is None:
            return await run()

        # One learner, one model call at a time. The lock prevents parallel
        # calls; the quota itself reads committed audit rows, so a burst can
        # still overrun the soft cap slightly until the first transactions
        # commit. A hard cap needs an atomic reservation (see security.md).
        async with self._locks.hold(user_id):
            return await run()

    async def _enforce(self, user_id: int | None, purpose: str) -> None:
        if not any(
            (
                self._settings.max_calls_per_day,
                self._settings.max_example_calls_per_day,
                self._settings.max_tokens_per_day,
                self._settings.max_tokens_per_day_global,
            )
        ):
            return

        if self._settings.max_tokens_per_day_global:
            await self._enforce_global_budget()

        if user_id is None:
            return

        now = utcnow()
        usage = await self._calls.usage_since(
            user_id,
            since=now - WINDOW,
            background_purpose=EXAMPLE_PURPOSE,
        )
        interactive_calls = usage["calls"] - usage["background_calls"]

        if purpose == EXAMPLE_PURPOSE:
            if (
                self._settings.max_example_calls_per_day
                and usage["background_calls"] >= self._settings.max_example_calls_per_day
            ):
                self._refuse(
                    user_id,
                    "example_calls_per_day",
                    usage["background_calls"],
                    self._settings.max_example_calls_per_day,
                )
        elif (
            self._settings.max_calls_per_day
            and interactive_calls >= self._settings.max_calls_per_day
        ):
            self._refuse(
                user_id,
                "calls_per_day",
                interactive_calls,
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

    async def _enforce_global_budget(self) -> None:
        """Alert on and enforce the bot-wide budget, learner or no learner."""
        limit = self._settings.max_tokens_per_day_global
        if limit <= 0:
            return
        tokens = await self._global_tokens()
        await self._budget.check(tokens, self._settings)
        if tokens >= limit:
            raise LlmGlobalBudgetExceededError(
                f"global token budget reached: {tokens} used, limit {limit}"
            )

    async def _global_tokens(self) -> int:
        """The bot-wide token total, from the cache or one SUM when it is stale.

        The miss path is single-flight: every reader that finds the cache cold
        queues on one lock, the first one queries and remembers, and the rest
        find its value on the second check. A burst therefore costs one SUM per
        process and TTL, not one per waiting handler.
        """
        now = self._clock()
        cached = self._usage_cache.read(now)
        if cached is not None:
            return cached

        async with self._usage_cache.lock():
            now = self._clock()
            cached = self._usage_cache.read(now)
            if cached is not None:
                return cached

            usage = await self._calls.global_usage_since(since=utcnow() - WINDOW)
            tokens = usage["tokens"]
            self._usage_cache.remember(tokens, now)
            return tokens

    @staticmethod
    def _refuse(user_id: int, window: str, used: int, limit: int) -> NoReturn:
        # A stable event name: this is the anomaly an operator wants an alert on.
        log.warning("llm_quota_exceeded", user_id=user_id, window=window, used=used, limit=limit)
        raise LlmQuotaExceededError(f"{window} limit reached: {used} used, limit {limit}")
