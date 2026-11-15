"""Retention of the LLM audit table.

The table is the source of both the per-learner quota and the operator's cost
report, so it is kept long enough for a review and no longer than configured.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.config import Settings
from eng_remember.models.llm_call import LlmCall
from eng_remember.repositories.llm_calls import LlmCallRecord, LlmCallRepository
from eng_remember.utils.time import utcnow


def test_retention_defaults_to_ninety_days() -> None:
    assert Settings(_env_file=None).llm_call_retention_days == 90


def test_retention_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_call_retention_days=-1)


def test_zero_means_retention_is_off() -> None:
    assert Settings(_env_file=None, llm_call_retention_days=0).llm_call_retention_days == 0


async def test_delete_older_than_prunes_only_old(session: AsyncSession) -> None:
    """Rows older than the cutoff go; a fresh audit trail stays intact."""
    repository = LlmCallRepository(session)
    old = await repository.add(
        LlmCallRecord(purpose="parse_message", provider_model="model", success=True)
    )
    fresh = await repository.add(
        LlmCallRecord(purpose="parse_message", provider_model="model", success=True)
    )
    now = utcnow()
    await session.execute(
        update(LlmCall).where(LlmCall.id == old.id).values(created_at=now - timedelta(days=91))
    )
    await session.execute(
        update(LlmCall).where(LlmCall.id == fresh.id).values(created_at=now - timedelta(days=89))
    )
    await session.commit()

    removed = await repository.delete_older_than(now - timedelta(days=90))
    await session.commit()

    assert removed == 1
    remaining = (await session.scalars(select(LlmCall.id))).all()
    assert remaining == [fresh.id]


async def test_nothing_old_returns_zero(session: AsyncSession) -> None:
    repository = LlmCallRepository(session)
    await repository.add(
        LlmCallRecord(purpose="parse_message", provider_model="model", success=True)
    )
    await session.commit()

    removed = await repository.delete_older_than(utcnow() - timedelta(days=90))

    assert removed == 0
