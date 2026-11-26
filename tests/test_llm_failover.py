"""Failover between the primary and the standby model route."""

from __future__ import annotations

import asyncio
from typing import TypeVar, cast

import pytest
from pydantic import BaseModel

from eng_remember.errors import (
    LlmGlobalBudgetExceededError,
    LlmNotConfiguredError,
    LlmQuotaExceededError,
    LlmResponseError,
)
from eng_remember.services.llm.client import FailoverClient, complete_json_with_model

ModelT = TypeVar("ModelT", bound=BaseModel)


class Answer(BaseModel):
    route: str


class Route:
    """A model route that answers or fails with a chosen error."""

    def __init__(self, name: str, *, error: Exception | None = None) -> None:
        self._name = name
        self._error = error
        self.calls = 0

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def available(self) -> bool:
        return True

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: type[ModelT],
        user_id: int | None = None,
    ) -> ModelT:
        del purpose, system, user, schema, user_id
        self.calls += 1
        if self._error is not None:
            raise self._error
        return cast(ModelT, Answer(route=self._name))


async def ask(client: FailoverClient) -> Answer:
    return await client.complete_json(purpose="parse_message", system="s", user="u", schema=Answer)


@pytest.mark.parametrize(
    "error",
    [
        LlmResponseError("provider is down"),
        LlmNotConfiguredError("primary key was revoked"),
    ],
)
async def test_a_provider_failure_switches_to_the_standby(error: Exception) -> None:
    primary = Route("primary", error=error)
    fallback = Route("fallback")
    client = FailoverClient(primary, fallback)

    result = await ask(client)

    assert result.route == "fallback"
    assert primary.calls == 1
    assert fallback.calls == 1


@pytest.mark.parametrize(
    "error",
    [
        LlmQuotaExceededError("learner is over the limit"),
        LlmGlobalBudgetExceededError("bot budget is spent"),
    ],
)
async def test_our_own_refusals_are_not_bypassed(error: Exception) -> None:
    primary = Route("primary", error=error)
    fallback = Route("fallback")
    client = FailoverClient(primary, fallback)

    with pytest.raises(type(error)):
        await ask(client)

    assert fallback.calls == 0


async def test_without_a_primary_the_standby_is_the_route() -> None:
    fallback = Route("fallback")
    client = FailoverClient(None, fallback)

    assert client.available
    assert client.model_name == "fallback"
    assert (await ask(client)).route == "fallback"


async def test_a_standby_failure_surfaces_its_own_error() -> None:
    primary = Route("primary", error=LlmResponseError("primary is down"))
    fallback = Route("fallback", error=LlmResponseError("standby is down"))
    client = FailoverClient(primary, fallback)

    with pytest.raises(LlmResponseError, match="standby is down"):
        await ask(client)

    assert primary.calls == 1
    assert fallback.calls == 1


async def test_overlapping_requests_keep_their_own_model_names() -> None:
    class Primary(Route):
        async def complete_json(
            self,
            *,
            purpose: str,
            system: str,
            user: str,
            schema: type[ModelT],
            user_id: int | None = None,
        ) -> ModelT:
            await asyncio.sleep(0)
            if user == "needs standby":
                raise LlmResponseError("provider is down")
            return await super().complete_json(
                purpose=purpose, system=system, user=user, schema=schema, user_id=user_id
            )

    client = FailoverClient(Primary("primary"), Route("standby"))
    results = await asyncio.gather(
        complete_json_with_model(
            client, purpose="parse_message", system="rules", user="needs standby", schema=Answer
        ),
        complete_json_with_model(
            client, purpose="parse_message", system="rules", user="another request", schema=Answer
        ),
    )

    assert [(answer.route, model) for answer, model in results] == [
        ("standby", "standby"),
        ("primary", "primary"),
    ]


def test_at_least_one_route_is_required() -> None:
    with pytest.raises(ValueError):
        FailoverClient(None, None)
