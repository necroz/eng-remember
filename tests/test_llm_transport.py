"""One LLM transport shared by every request, instead of one per update.

The transport owns the HTTP client and what the provider has taught us: whether
it understands ``response_format`` and ``reasoning_effort``. A per-request client
is only a thin view over it, so a rejection learned in one update is not paid for
again in the next.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2
import openai
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.config import LlmSettings, Settings
from eng_remember.services.container import build_services
from eng_remember.services.llm import client as client_module
from eng_remember.services.llm.client import LlmTransport, OpenAiCompatibleClient
from eng_remember.services.llm.schema import Intent, ParseResponse
from tests.factories import completion


class FakeEndpoint:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("the transport made more calls than the test prepared")
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


class GatedRejectionEndpoint:
    """Reject an optional parameter only once two requests are in flight.

    The first rejected call waits until a second call has recorded its kwargs,
    so both requests are guaranteed to have built their parameters before the
    provider's first 400 lands. That is exactly the interleaving where a flag
    learned by one request used to make the other fail instead of degrading.
    """

    def __init__(self, parameter: str, message: str) -> None:
        self._parameter = parameter
        self._message = message
        self.calls: list[dict[str, Any]] = []
        self._rejections = 0
        self._both_in_flight = asyncio.Event()

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._parameter in kwargs:
            self._rejections += 1
            if self._rejections == 1:
                await self._both_in_flight.wait()
            else:
                self._both_in_flight.set()
            raise bad_request(self._message)
        return completion('{"intent": "unknown"}')


def fake_with_endpoint(endpoint: Any) -> FakeOpenAi:
    fake = FakeOpenAi([])
    fake.endpoint = endpoint
    fake.chat = FakeChat(endpoint)
    return fake


def llm_settings(**overrides: Any) -> LlmSettings:
    base: dict[str, Any] = {
        "api_key": SecretStr("test-key"),
        "model": "test-model",
        "retries": 0,
        "timeout_seconds": 5.0,
    }
    base.update(overrides)
    return LlmSettings(**base)


def configured(settings: Settings, **overrides: Any) -> Settings:
    return settings.model_copy(update={"llm": llm_settings(**overrides)})


def http_response(status: int) -> httpx2.Response:
    return httpx2.Response(
        status,
        request=httpx2.Request("POST", "http://localhost:11434/v1/chat/completions"),
        text="{}",
    )


def bad_request(message: str) -> Exception:
    return openai.BadRequestError(message, response=http_response(400), body=None)


async def complete(client: OpenAiCompatibleClient) -> ParseResponse:
    return await client.complete_json(
        purpose="parse_message",
        system="system",
        user="user",
        schema=ParseResponse,
    )


@pytest.fixture
def async_openai_factory(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace the SDK constructor and remember how often it was called."""
    created: list[FakeOpenAi] = []
    responses: list[Any] = []
    fake = FakeOpenAi(responses)

    def factory(**kwargs: Any) -> FakeOpenAi:
        del kwargs
        created.append(fake)
        return fake

    monkeypatch.setattr(client_module, "AsyncOpenAI", factory)
    return fake, created


async def test_transport_shared_across_updates(
    session: AsyncSession,
    settings: Settings,
    async_openai_factory: Any,
) -> None:
    """Two updates build two clients but reach the provider through one transport."""
    fake, created = async_openai_factory
    fake.endpoint._responses.extend(
        [completion('{"intent": "unknown"}'), completion('{"intent": "unknown"}')]
    )
    process_settings = configured(settings)
    transport = LlmTransport(process_settings.llm)

    first = build_services(session, process_settings, llm_transport=transport)
    second = build_services(session, process_settings, llm_transport=transport)

    await first.parser.parse("run")
    await second.parser.parse("run")

    assert len(created) == 1
    assert len(fake.endpoint.calls) == 2


async def test_learned_rejection_persists_between_per_request_clients() -> None:
    """A 400 in one update teaches the process-wide transport for the next one."""
    fake = FakeOpenAi(
        [
            bad_request("Unsupported parameter: reasoning_effort"),
            completion('{"intent": "unknown"}'),
            completion('{"intent": "unknown"}'),
        ]
    )
    transport = LlmTransport(llm_settings(reasoning_effort="none"), client=fake)

    first = OpenAiCompatibleClient(
        llm_settings(reasoning_effort="none"),
        transport=transport,
    )
    await complete(first)

    second = OpenAiCompatibleClient(
        llm_settings(reasoning_effort="none"),
        transport=transport,
    )
    await complete(second)

    assert fake.endpoint.calls[0]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in fake.endpoint.calls[1]
    assert "reasoning_effort" not in fake.endpoint.calls[2]


async def test_concurrent_rejections_of_response_format_both_degrade() -> None:
    """Two updates in flight when the provider first rejects JSON mode.

    One request learns the lesson first; the other sent ``response_format`` too,
    so it must retry without the parameter rather than surface a 400.
    """
    endpoint = GatedRejectionEndpoint("response_format", "Unsupported parameter: response_format")
    settings = llm_settings()
    transport = LlmTransport(settings, client=fake_with_endpoint(endpoint))
    first = OpenAiCompatibleClient(settings, transport=transport)
    second = OpenAiCompatibleClient(settings, transport=transport)

    results = await asyncio.wait_for(asyncio.gather(complete(first), complete(second)), timeout=5)

    assert [result.intent for result in results] == [Intent.UNKNOWN, Intent.UNKNOWN]
    # Both original calls carried the parameter; both retries omitted it.
    assert sum("response_format" in call for call in endpoint.calls) == 2
    assert len(endpoint.calls) == 4
    assert transport._json_mode is False


async def test_concurrent_rejections_of_reasoning_effort_both_degrade() -> None:
    """Same interleaving for the reasoning knob."""
    endpoint = GatedRejectionEndpoint("reasoning_effort", "Unsupported parameter: reasoning_effort")
    settings = llm_settings(reasoning_effort="low")
    transport = LlmTransport(settings, client=fake_with_endpoint(endpoint))
    first = OpenAiCompatibleClient(settings, transport=transport)
    second = OpenAiCompatibleClient(settings, transport=transport)

    results = await asyncio.wait_for(asyncio.gather(complete(first), complete(second)), timeout=5)

    assert [result.intent for result in results] == [Intent.UNKNOWN, Intent.UNKNOWN]
    assert sum("reasoning_effort" in call for call in endpoint.calls) == 2
    assert len(endpoint.calls) == 4
    assert transport._reasoning_effort is None


async def test_default_config_sends_no_reasoning_effort() -> None:
    """The parameter is opt-in: an unset value is not sent at all."""
    assert LlmSettings(api_key=SecretStr("test-key")).reasoning_effort is None
    fake = FakeOpenAi([completion('{"intent": "unknown"}')])
    transport = LlmTransport(llm_settings(), client=fake)
    client = OpenAiCompatibleClient(llm_settings(), transport=transport)

    await complete(client)

    assert "reasoning_effort" not in fake.endpoint.calls[0]


async def test_explicit_reasoning_effort_is_sent() -> None:
    fake = FakeOpenAi([completion('{"intent": "unknown"}')])
    transport = LlmTransport(llm_settings(reasoning_effort="low"), client=fake)
    client = OpenAiCompatibleClient(llm_settings(reasoning_effort="low"), transport=transport)

    await complete(client)

    assert fake.endpoint.calls[0]["reasoning_effort"] == "low"


async def test_transport_aclose_closes_underlying_but_request_does_not() -> None:
    fake = FakeOpenAi([])
    transport = LlmTransport(llm_settings(), client=fake)
    request_client = OpenAiCompatibleClient(llm_settings(), transport=transport)

    await request_client.aclose()
    assert not fake.closed

    await transport.aclose()
    assert fake.closed


async def test_client_without_transport_owns_one() -> None:
    """Isolation path: a client created on its own closes what it created."""
    fake = FakeOpenAi([])
    client = OpenAiCompatibleClient(llm_settings(), client=fake)

    await client.aclose()

    assert fake.closed
