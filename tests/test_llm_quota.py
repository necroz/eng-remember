"""Per-learner cost limits in front of every model call.

The gate reads its numbers from the same audit table the operator sees, so the
tests build their usage by writing audit rows — the production path — rather
than by mocking the repository.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.config import LlmSettings, Settings
from eng_remember.errors import LlmGlobalBudgetExceededError, LlmQuotaExceededError
from eng_remember.models.user import User
from eng_remember.repositories.llm_calls import LlmCallRecord, LlmCallRepository
from eng_remember.repositories.users import TelegramProfile, UserRepository
from eng_remember.services.llm.budget import GlobalBudgetMonitor
from eng_remember.services.llm.factory import build_llm_client
from eng_remember.services.llm.quota import QuotaLimitedClient
from eng_remember.services.llm.schema import ParseResponse


class RecordingModel:
    """A ``JsonChatClient`` that counts calls and answers with an empty payload."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "recording-model"

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
        return schema()


async def record_usage(
    session: AsyncSession,
    user_id: int,
    *,
    calls: int = 0,
    tokens_per_call: int = 0,
    purpose: str = "parse_message",
) -> None:
    repository = LlmCallRepository(session)
    for _ in range(calls):
        await repository.add(
            LlmCallRecord(
                purpose=purpose,
                provider_model="recording-model",
                success=True,
                user_id=user_id,
                total_tokens=tokens_per_call,
            )
        )
    await session.commit()


async def make_call(
    client: QuotaLimitedClient,
    user_id: int,
    *,
    purpose: str = "parse_message",
) -> None:
    await client.complete_json(
        purpose=purpose,
        system="system",
        user="user",
        schema=ParseResponse,
        user_id=user_id,
    )


async def test_calls_below_the_limit_reach_the_model(
    session: AsyncSession,
    user: User,
) -> None:
    await record_usage(session, user.id, calls=2)
    inner = RecordingModel()
    client = QuotaLimitedClient(
        inner,
        calls=LlmCallRepository(session),
        settings=LlmSettings(max_calls_per_day=3, max_tokens_per_day=0),
    )

    await make_call(client, user.id)

    assert inner.calls == 1


async def test_the_next_call_over_the_daily_limit_is_refused(
    session: AsyncSession,
    user: User,
) -> None:
    await record_usage(session, user.id, calls=3)
    inner = RecordingModel()
    client = QuotaLimitedClient(
        inner,
        calls=LlmCallRepository(session),
        settings=LlmSettings(max_calls_per_day=3, max_tokens_per_day=0),
    )

    with pytest.raises(LlmQuotaExceededError):
        await make_call(client, user.id)

    assert inner.calls == 0


async def test_the_daily_token_budget_is_enforced(
    session: AsyncSession,
    user: User,
) -> None:
    await record_usage(session, user.id, calls=1, tokens_per_call=5000)
    inner = RecordingModel()
    client = QuotaLimitedClient(
        inner,
        calls=LlmCallRepository(session),
        settings=LlmSettings(max_calls_per_day=0, max_tokens_per_day=5000),
    )

    with pytest.raises(LlmQuotaExceededError):
        await make_call(client, user.id)

    assert inner.calls == 0


async def test_example_calls_have_their_own_daily_budget(
    session: AsyncSession,
    user: User,
) -> None:
    await record_usage(session, user.id, calls=30, purpose="card_examples")
    inner = RecordingModel()
    client = QuotaLimitedClient(
        inner,
        calls=LlmCallRepository(session),
        settings=LlmSettings(
            max_calls_per_day=60,
            max_example_calls_per_day=30,
            max_tokens_per_day=0,
            max_tokens_per_day_global=0,
        ),
    )

    with pytest.raises(LlmQuotaExceededError):
        await make_call(client, user.id, purpose="card_examples")

    assert inner.calls == 0


async def test_example_calls_do_not_consume_the_interactive_budget(
    session: AsyncSession,
    user: User,
) -> None:
    await record_usage(session, user.id, calls=30, purpose="card_examples")
    await record_usage(session, user.id, calls=59)
    inner = RecordingModel()
    client = QuotaLimitedClient(
        inner,
        calls=LlmCallRepository(session),
        settings=LlmSettings(
            max_calls_per_day=60,
            max_example_calls_per_day=30,
            max_tokens_per_day=0,
            max_tokens_per_day_global=0,
        ),
    )

    await make_call(client, user.id)

    assert inner.calls == 1


async def test_global_token_budget_refuses_even_under_the_user_limit(
    session: AsyncSession,
    user: User,
) -> None:
    other, _ = await UserRepository(session).get_or_create(
        TelegramProfile(telegram_id=user.telegram_id + 1)
    )
    await session.commit()
    await record_usage(session, other.id, calls=1, tokens_per_call=1000)
    inner = RecordingModel()
    client = QuotaLimitedClient(
        inner,
        calls=LlmCallRepository(session),
        settings=LlmSettings(
            max_calls_per_day=60,
            max_example_calls_per_day=30,
            max_tokens_per_day=0,
            max_tokens_per_day_global=1000,
        ),
        budget=GlobalBudgetMonitor(),
    )

    with pytest.raises(LlmGlobalBudgetExceededError):
        await make_call(client, user.id)

    assert inner.calls == 0


async def test_calls_older_than_a_day_do_not_count(
    session: AsyncSession,
    user: User,
) -> None:
    from datetime import timedelta

    from eng_remember.models.llm_call import LlmCall
    from eng_remember.utils.time import utcnow

    session.add(
        LlmCall(
            user_id=user.id,
            purpose="parse_message",
            provider_model="recording-model",
            total_tokens=10,
            created_at=utcnow() - timedelta(days=2),
        )
    )
    await session.commit()
    inner = RecordingModel()
    client = QuotaLimitedClient(
        inner,
        calls=LlmCallRepository(session),
        settings=LlmSettings(max_calls_per_day=1, max_tokens_per_day=0),
    )

    await make_call(client, user.id)

    assert inner.calls == 1


async def test_usage_of_another_learner_does_not_count(
    session: AsyncSession,
    user: User,
) -> None:
    other, _ = await UserRepository(session).get_or_create(
        TelegramProfile(telegram_id=user.telegram_id + 1)
    )
    await session.commit()
    await record_usage(session, other.id, calls=5)
    inner = RecordingModel()
    client = QuotaLimitedClient(
        inner,
        calls=LlmCallRepository(session),
        settings=LlmSettings(max_calls_per_day=3, max_tokens_per_day=0),
    )

    await make_call(client, user.id)

    assert inner.calls == 1


async def test_zero_limits_disables_the_check(
    session: AsyncSession,
    user: User,
) -> None:
    await record_usage(session, user.id, calls=100, tokens_per_call=1_000_000)
    inner = RecordingModel()
    client = QuotaLimitedClient(
        inner,
        calls=LlmCallRepository(session),
        settings=LlmSettings(
            max_calls_per_day=0,
            max_example_calls_per_day=0,
            max_tokens_per_day=0,
            max_tokens_per_day_global=0,
        ),
    )

    await make_call(client, user.id)

    assert inner.calls == 1


def test_the_factory_wraps_the_client_in_the_gate(
    session: AsyncSession,
    settings: Settings,
) -> None:
    configured = settings.model_copy(update={"llm": LlmSettings(api_key=SecretStr("test-key"))})

    client = build_llm_client(session, configured)

    assert isinstance(client, QuotaLimitedClient)
