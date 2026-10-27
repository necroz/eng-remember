"""Per-learner model calls are serialized before they reach the provider.

The lock lives in the quota gate, not in the update middleware: a pause or a
menu tap must still work while a model check is in flight, but two model calls
for the same learner must not overlap.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.config import LlmSettings
from eng_remember.models.user import User
from eng_remember.repositories.llm_calls import LlmCallRepository
from eng_remember.repositories.users import TelegramProfile, UserRepository
from eng_remember.services.llm.quota import QuotaLimitedClient
from eng_remember.services.llm.schema import ParseResponse
from eng_remember.utils.user_locks import UserLockRegistry


class GatedModel:
    """A model that holds every call until the test releases it."""

    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def model_name(self) -> str:
        return "gated-model"

    @property
    def available(self) -> bool:
        return True

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: Any,
        user_id: int | None = None,
    ) -> Any:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        await self.release.wait()
        self.active -= 1
        return schema()


class BarrierModel:
    """A model that waits until a given number of calls are inside at once."""

    def __init__(self, parties: int) -> None:
        self.parties = parties
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.entered = asyncio.Event()
        self._inside = 0

    @property
    def model_name(self) -> str:
        return "barrier-model"

    @property
    def available(self) -> bool:
        return True

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: Any,
        user_id: int | None = None,
    ) -> Any:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self._inside += 1
        if self._inside >= self.parties:
            self.entered.set()
        await self.entered.wait()
        self.active -= 1
        return schema()


class FailingModel:
    @property
    def model_name(self) -> str:
        return "failing-model"

    @property
    def available(self) -> bool:
        return True

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: Any,
        user_id: int | None = None,
    ) -> Any:
        raise RuntimeError("boom")


def make_client(
    session: AsyncSession,
    model: Any,
    locks: UserLockRegistry,
) -> QuotaLimitedClient:
    return QuotaLimitedClient(
        model,
        calls=LlmCallRepository(session),
        settings=LlmSettings(max_calls_per_day=0, max_tokens_per_day=0),
        locks=locks,
    )


async def make_call(client: QuotaLimitedClient, user_id: int) -> None:
    await client.complete_json(
        purpose="parse_message",
        system="system",
        user="user",
        schema=ParseResponse,
        user_id=user_id,
    )


async def test_registry_serializes_tasks_for_the_same_user() -> None:
    registry = UserLockRegistry()
    active = 0
    max_active = 0

    async def worker() -> None:
        nonlocal active, max_active
        async with registry.hold(1):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(3)))

    assert max_active == 1
    assert registry.size == 0


async def test_same_user_calls_wait_for_each_other(
    session: AsyncSession,
    user: User,
) -> None:
    locks = UserLockRegistry()
    model = GatedModel()
    client = make_client(session, model, locks)

    first = asyncio.create_task(make_call(client, user.id))
    await model.entered.wait()
    second = asyncio.create_task(make_call(client, user.id))
    await asyncio.sleep(0.01)

    assert model.calls == 1

    model.release.set()
    await asyncio.gather(first, second)

    assert model.calls == 2
    assert model.max_active == 1
    assert locks.size == 0


async def test_different_users_call_concurrently(
    session: AsyncSession,
    user: User,
) -> None:
    other, _ = await UserRepository(session).get_or_create(
        TelegramProfile(telegram_id=user.telegram_id + 1)
    )
    await session.commit()
    locks = UserLockRegistry()
    model = BarrierModel(parties=2)
    client = make_client(session, model, locks)

    first = asyncio.create_task(make_call(client, user.id))
    second = asyncio.create_task(make_call(client, other.id))
    await asyncio.wait_for(model.entered.wait(), timeout=1)

    assert model.max_active == 2
    await asyncio.gather(first, second)
    assert locks.size == 0


async def test_the_lock_is_released_when_the_model_raises(
    session: AsyncSession,
    user: User,
) -> None:
    locks = UserLockRegistry()
    client = make_client(session, FailingModel(), locks)

    with pytest.raises(RuntimeError):
        await make_call(client, user.id)

    assert locks.size == 0
