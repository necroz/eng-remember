"""Behaviour of the OpenAI-compatible client.

The HTTP layer is replaced by a fake that returns prepared answers, so these
tests cover the parts this project actually owns: retry policy, repairing a
malformed answer, tolerating providers without JSON mode, and the audit records
that make a bad parse debuggable.
"""

from __future__ import annotations

from typing import Any

import httpx2
import openai
import pytest
from pydantic import SecretStr

from eng_remember.config import LlmSettings
from eng_remember.errors import LlmNotConfiguredError, LlmResponseError
from eng_remember.repositories.llm_calls import LlmCallRecord
from eng_remember.services.llm.client import OpenAiCompatibleClient
from eng_remember.services.llm.schema import ParseResponse
from tests.factories import completion


class FakeEndpoint:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("the client made more calls than the test prepared")
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeChat:
    def __init__(self, endpoint: FakeEndpoint) -> None:
        self.completions = endpoint


class FakeOpenAi:
    def __init__(self, responses: list[Any]) -> None:
        self.endpoint = FakeEndpoint(responses)
        self.chat = FakeChat(self.endpoint)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def http_response(status: int, text: str = "{}") -> httpx2.Response:
    return httpx2.Response(
        status,
        request=httpx2.Request("POST", "http://localhost:11434/v1/chat/completions"),
        text=text,
    )


def api_error(kind: str, message: str, status: int) -> Exception:
    cls = {
        "bad_request": openai.BadRequestError,
        "rate_limit": openai.RateLimitError,
        "auth": openai.AuthenticationError,
        "server": openai.InternalServerError,
    }[kind]
    error: Exception = cls(message, response=http_response(status), body=None)
    return error


def llm_settings(**overrides: Any) -> LlmSettings:
    base: dict[str, Any] = {
        "api_key": SecretStr("test-key"),
        "model": "test-model",
        "retries": 2,
        "timeout_seconds": 5.0,
    }
    base.update(overrides)
    return LlmSettings(**base)


class Sink:
    def __init__(self) -> None:
        self.records: list[LlmCallRecord] = []

    async def __call__(self, record: LlmCallRecord) -> None:
        self.records.append(record)


def make_client(
    responses: list[Any],
    *,
    sink: Sink | None = None,
    **overrides: Any,
) -> tuple[OpenAiCompatibleClient, FakeOpenAi]:
    fake = FakeOpenAi(responses)
    client = OpenAiCompatibleClient(llm_settings(**overrides), sink=sink, client=fake)
    return client, fake


async def complete(client: OpenAiCompatibleClient) -> ParseResponse:
    return await client.complete_json(
        purpose="parse_message",
        system="system",
        user="user",
        schema=ParseResponse,
    )


async def test_valid_answer_is_parsed() -> None:
    sink = Sink()
    client, fake = make_client(
        [
            completion(
                '{"intent": "add_cards", "cards": [{"term": "run", "translation": "бежать"}]}'
            )
        ],
        sink=sink,
    )

    result = await complete(client)

    assert result.cards[0].term == "run"
    assert len(fake.endpoint.calls) == 1
    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.success
    assert record.attempt == 1
    assert record.prompt_tokens == 11
    assert record.total_tokens == 18
    assert record.latency_ms is not None
    assert record.purpose == "parse_message"
    assert record.provider_model == "test-model"


async def test_json_mode_is_requested() -> None:
    client, fake = make_client([completion('{"intent": "unknown"}')])

    await complete(client)

    assert fake.endpoint.calls[0]["response_format"] == {"type": "json_object"}
    assert fake.endpoint.calls[0]["model"] == "test-model"
    assert fake.endpoint.calls[0]["temperature"] == 0.0


async def test_fenced_answer_is_accepted() -> None:
    client, _ = make_client([completion('```json\n{"intent": "unknown"}\n```')])

    assert (await complete(client)).intent.value == "unknown"


async def test_malformed_answer_is_sent_back_with_the_error() -> None:
    sink = Sink()
    client, fake = make_client(
        [
            completion("not json at all"),
            completion(
                '{"intent": "add_cards", "cards": [{"term": "run", "translation": "бежать"}]}'
            ),
        ],
        sink=sink,
    )

    result = await complete(client)

    assert result.cards[0].term == "run"
    assert len(fake.endpoint.calls) == 2
    repair_messages = fake.endpoint.calls[1]["messages"]
    assert repair_messages[-1]["role"] == "user"
    assert "could not be used" in repair_messages[-1]["content"]
    assert [record.success for record in sink.records] == [False, True]
    assert [record.attempt for record in sink.records] == [1, 2]


async def test_schema_violation_also_triggers_a_repair() -> None:
    """A syntactically valid answer with unusable content must not be trusted."""
    client, fake = make_client(
        [
            completion('{"intent": "add_cards", "cards": "not-a-list"}'),
            completion('{"intent": "add_cards", "cards": []}'),
        ]
    )

    await complete(client)

    assert len(fake.endpoint.calls) == 2
    assert "ValidationError" in fake.endpoint.calls[1]["messages"][-1]["content"]


async def test_retries_are_bounded_and_reported() -> None:
    sink = Sink()
    client, fake = make_client([completion("nope")] * 3, sink=sink)

    with pytest.raises(LlmResponseError):
        await complete(client)

    assert len(fake.endpoint.calls) == 3  # retries=2 → three attempts
    assert all(not record.success for record in sink.records)
    assert len(sink.records) == 3


async def test_provider_without_json_mode_still_works() -> None:
    """Ollama and friends reject response_format; the call must survive it."""
    client, fake = make_client(
        [
            api_error("bad_request", "Unsupported parameter: response_format", 400),
            completion('{"intent": "unknown"}'),
        ]
    )

    result = await complete(client)

    assert result.intent.value == "unknown"
    assert len(fake.endpoint.calls) == 2
    assert "response_format" in fake.endpoint.calls[0]
    assert "response_format" not in fake.endpoint.calls[1]


async def test_json_mode_is_not_retried_twice() -> None:
    """Once disabled, later calls must not pay for the rejection again."""
    client, fake = make_client(
        [
            api_error("bad_request", "Unsupported parameter: response_format", 400),
            completion('{"intent": "unknown"}'),
            completion('{"intent": "unknown"}'),
        ]
    )

    await complete(client)
    await complete(client)

    assert "response_format" not in fake.endpoint.calls[2]


async def test_rate_limit_is_retried() -> None:
    client, fake = make_client(
        [api_error("rate_limit", "slow down", 429), completion('{"intent": "unknown"}')]
    )

    await complete(client)

    assert len(fake.endpoint.calls) == 2


async def test_server_error_is_retried_but_only_up_to_the_budget() -> None:
    client, fake = make_client([api_error("server", "boom", 500)] * 3)

    with pytest.raises(LlmResponseError):
        await complete(client)

    assert len(fake.endpoint.calls) == 3


async def test_bad_credentials_fail_immediately() -> None:
    sink = Sink()
    client, fake = make_client([api_error("auth", "invalid api key", 401)], sink=sink)

    with pytest.raises(LlmNotConfiguredError):
        await complete(client)

    assert len(fake.endpoint.calls) == 1
    assert len(sink.records) == 1
    assert "AuthenticationError" in (sink.records[0].error or "")


async def test_missing_key_never_calls_the_provider() -> None:
    client, fake = make_client([completion("{}")], api_key=None)

    assert not client.available
    with pytest.raises(LlmNotConfiguredError):
        await complete(client)

    assert fake.endpoint.calls == []


async def test_disabled_client_is_unavailable() -> None:
    client, _ = make_client([completion("{}")], enabled=False)

    assert not client.available


async def test_aclose_closes_the_underlying_client() -> None:
    client, fake = make_client([])

    await client.aclose()

    assert fake.closed
