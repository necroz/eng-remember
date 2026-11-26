"""OpenAI-compatible chat client with structured JSON output.

One implementation serves OpenAI, OpenRouter, DeepSeek, vLLM, Ollama and
anything else that speaks the ``/chat/completions`` protocol — only ``base_url``,
``api_key`` and ``model`` change.

Two design points worth keeping:

* The client never touches the database. Every attempt is reported through the
  ``sink`` callback, so the audit table stays an optional concern and this module
  is testable without a session.
* Nothing is retried blindly. A malformed answer is sent back to the model
  together with the validation error, which fixes almost all shape problems;
  transport errors get a bounded retry; authentication and quota problems fail
  immediately because retrying cannot help.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

import structlog
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    OpenAIError,
    RateLimitError,
)
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from eng_remember.config import LlmSettings
from eng_remember.errors import (
    LlmError,
    LlmGlobalBudgetExceededError,
    LlmNotConfiguredError,
    LlmQuotaExceededError,
    LlmResponseError,
)
from eng_remember.repositories.llm_calls import LlmCallRecord
from eng_remember.services.llm.prompts import REPAIR_TEMPLATE
from eng_remember.services.llm.schema import extract_json_object

__all__ = [
    "FailoverClient",
    "JsonChatClient",
    "LlmCallSink",
    "LlmTransport",
    "OpenAiCompatibleClient",
    "OpenAiLike",
    "complete_json_with_model",
]

log = structlog.get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)
LlmCallSink = Callable[[LlmCallRecord], Awaitable[None]]


class _CompletionEndpoint(Protocol):
    # Read-only properties all the way down, and the method as
    # ``Callable[..., Awaitable[Any]]``. The SDK's ``create`` is an overload whose
    # parameters are spelled out, so only the ``...`` callable accepts it — and a
    # read-only member is what a method is seen as structurally.
    @property
    def create(self) -> Callable[..., Awaitable[Any]]: ...


class _ChatNamespace(Protocol):
    @property
    def completions(self) -> _CompletionEndpoint: ...


class OpenAiLike(Protocol):
    """The slice of ``AsyncOpenAI`` this client actually uses.

    Declared explicitly so the test suite can inject a recording fake with no
    HTTP server involved, and so swapping the SDK later is a type-checked change
    rather than a surprise at runtime.
    """

    @property
    def chat(self) -> _ChatNamespace: ...

    async def close(self) -> None: ...


# Transport-level failures worth one more try.
_RETRIABLE = (APIConnectionError, APITimeoutError, RateLimitError)

# A monthly plan that is out of quota is not coming back inside the retry
# budget: retrying the same key only adds latency and audit rows. The failover
# route (or the caller's fallback to rules) should react after one attempt.
_EXHAUSTED_QUOTA_CODE = "insufficient_quota"


def _is_exhausted_quota(exc: OpenAIError) -> bool:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code") == _EXHAUSTED_QUOTA_CODE:
            return True
        if body.get("code") == _EXHAUSTED_QUOTA_CODE:
            return True
    return _EXHAUSTED_QUOTA_CODE in str(exc)


def _provider_error(exc: OpenAIError, *, exhausted_quota: bool = False) -> str:
    # Provider messages can echo the request, including authentication data.
    category = type(exc).__name__
    if exhausted_quota:
        return f"{category}: {_EXHAUSTED_QUOTA_CODE}"
    if isinstance(exc, APIStatusError):
        return f"{category}: HTTP {exc.status_code}"
    return category


def _invalid_response_error(exc: json.JSONDecodeError | ValidationError) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"JSONDecodeError: line {exc.lineno}, column {exc.colno}"
    # ValidationError's string and message can contain the original input.
    errors = exc.errors(include_input=False, include_context=False, include_url=False)
    issues = []
    for error in errors:
        # Unknown field names are themselves part of the model's response.
        location = error["loc"][:-1] if error["type"] == "extra_forbidden" else error["loc"]
        path = ".".join(map(str, location)) or "<root>"
        issues.append(f"{path}: {error['type']}")
    return "ValidationError: " + "; ".join(issues)


# Failures where a retry is guaranteed to fail the same way.
_FATAL = (AuthenticationError,)

# The model's own bad answer is echoed back for repair, but only this much of
# it: a runaway 5000-character reply costs tokens on every later attempt without
# helping the model fix the shape.
_REPAIR_ECHO_LIMIT = 2000
_REPAIR_TRUNCATED_MARK = " …(обрезано)"

# A rejection is a lesson only when it names the parameter itself. Matching a
# bare word like "reasoning" would disable the knob for the whole process after
# an unrelated error such as "max_tokens must be greater than reasoning tokens".
_REASONING_EFFORT_REJECTION = re.compile(r"reasoning[_\s-]?effort")
_RESPONSE_FORMAT_REJECTION = re.compile(r"response[_\s-]?format")
_JSON_SCHEMA_REJECTION = re.compile(r"json[_\s-]?schema|invalid schema|structured output")


def _rejection_text(exc: BadRequestError) -> str:
    """Everything the provider said about a rejection, lowercased.

    The structured body wins: it names the offending parameter and nothing else.
    The raw ``str(exc)`` is a last resort because some providers echo the whole
    request in it, and a request that merely *mentions* ``response_format`` in
    some unrelated field must not teach the transport a lesson.
    """
    parts: list[str] = []
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            parts.extend(
                value
                for key in ("message", "param", "code", "type")
                if isinstance(value := error.get(key), str)
            )
        parts.extend(
            value for key in ("message", "param", "code") if isinstance(value := body.get(key), str)
        )
    if not parts:
        parts.append(str(exc))
    return " ".join(parts).lower()


def _strict_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic's schema, adjusted to the shape OpenAI's strict mode accepts.

    Strict mode wants every object to forbid extra keys and to list every
    property as required; optionality is expressed in the type (a null union),
    not by omitting the key. Pydantic already emits ``anyOf`` for ``X | None``,
    so the conversion is structural rather than semantic.
    """
    document = copy.deepcopy(schema.model_json_schema())

    def tighten(node: Any) -> Any:
        if isinstance(node, list):
            return [tighten(item) for item in node]
        if not isinstance(node, dict):
            return node
        if node.get("type") == "object" or "properties" in node:
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["properties"] = {key: tighten(value) for key, value in properties.items()}
                node["required"] = list(properties)
            node["additionalProperties"] = False
        for key in ("items", "additionalProperties"):
            if isinstance(node.get(key), dict):
                node[key] = tighten(node[key])
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            if isinstance(node.get(key), list):
                node[key] = [tighten(item) for item in node[key]]
        return node

    tightened = tighten(document)
    definitions = tightened.get("$defs")
    if isinstance(definitions, dict):
        tightened["$defs"] = {name: tighten(value) for name, value in definitions.items()}
    return cast(dict[str, Any], tightened)


def _echo_for_repair(content: str) -> str:
    """The model's answer as it goes back into the repair conversation."""
    if len(content) <= _REPAIR_ECHO_LIMIT:
        return content
    keep = _REPAIR_ECHO_LIMIT - len(_REPAIR_TRUNCATED_MARK)
    return f"{content[:keep]}{_REPAIR_TRUNCATED_MARK}"


@runtime_checkable
class JsonChatClient(Protocol):
    """What the rest of the project needs from a language model."""

    @property
    def model_name(self) -> str: ...

    @property
    def available(self) -> bool: ...

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: type[ModelT],
        user_id: int | None = None,
    ) -> ModelT: ...


@runtime_checkable
class _ModelAwareClient(Protocol):
    async def complete_json_with_model(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: type[ModelT],
        user_id: int | None = None,
    ) -> tuple[ModelT, str]: ...


async def complete_json_with_model[ModelT: BaseModel](
    client: JsonChatClient,
    *,
    purpose: str,
    system: str,
    user: str,
    schema: type[ModelT],
    user_id: int | None = None,
) -> tuple[ModelT, str]:
    """Return the response together with the model that produced it."""
    if isinstance(client, _ModelAwareClient):
        return await client.complete_json_with_model(
            purpose=purpose, system=system, user=user, schema=schema, user_id=user_id
        )
    result = await client.complete_json(
        purpose=purpose, system=system, user=user, schema=schema, user_id=user_id
    )
    return result, client.model_name


class LlmTransport:
    """The process-wide HTTP side of the model: one client, one set of lessons.

    Created once at startup and shared by every request, so the (lazily built)
    ``AsyncOpenAI`` connection pool is reused instead of leaking one client per
    update. It also owns the flags learned from provider rejections: once a
    provider says it does not understand ``response_format`` or
    ``reasoning_effort``, every later request remembers that instead of paying
    for the same 400 again. Structured outputs degrade in two steps: a provider
    that rejects only the strict JSON schema still gets plain ``json_object``.

    Tests can pass a ready-made ``client``; production leaves it ``None`` and the
    real SDK is built on first use.
    """

    def __init__(self, settings: LlmSettings, *, client: OpenAiLike | None = None) -> None:
        self._settings = settings
        self._client = client
        # Flipped off for the lifetime of the transport the first time a
        # provider rejects ``response_format``; small self-hosted servers often do.
        self._json_mode = True
        # A stricter form of the same knob: a schema-constrained answer. Not
        # every provider that understands ``json_object`` understands it, so a
        # rejection here only costs us the schema, not JSON mode itself.
        self._json_schema = True
        # Same idea for reasoning control: not every OpenAI-compatible endpoint
        # knows the parameter, and one rejected request is enough to learn that.
        self._reasoning_effort = (settings.reasoning_effort or "").strip() or None

    async def aclose(self) -> None:
        """Close the underlying client, if one was ever built."""
        client = self._client
        self._client = None
        if client is not None:
            await client.close()

    async def create(
        self,
        messages: list[ChatCompletionMessageParam],
        *,
        schema: type[BaseModel] | None = None,
    ) -> Any:
        """Call the endpoint, degrading gracefully when optional knobs are rejected."""
        api = self._api()
        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "messages": messages,
            "temperature": self._settings.temperature,
            "max_tokens": self._settings.max_output_tokens,
        }
        if self._json_schema and schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": _strict_json_schema(schema),
                    "strict": True,
                },
            }
        elif self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        while True:
            try:
                return await api.chat.completions.create(**kwargs)
            except BadRequestError as exc:
                rejection = _rejection_text(exc)
                changed = False
                # Degrade on what *this* request sent, not on the transport's
                # current flags: a concurrent request may have taught the
                # transport already, while our own kwargs still carry the
                # rejected parameter. The retry must drop it here too.
                response_format = kwargs.get("response_format")
                if isinstance(response_format, dict):
                    kind = response_format.get("type")
                    schema_rejected = _JSON_SCHEMA_REJECTION.search(rejection)
                    format_rejected = _RESPONSE_FORMAT_REJECTION.search(rejection)
                    if kind == "json_schema" and (schema_rejected or format_rejected):
                        self._json_schema = False
                        if schema_rejected and self._json_mode:
                            # The schema was the problem, not JSON mode: keep
                            # the weaker guarantee and lose only the shape.
                            kwargs["response_format"] = {"type": "json_object"}
                        else:
                            self._json_mode = False
                            kwargs.pop("response_format", None)
                        changed = True
                    elif kind == "json_object" and format_rejected:
                        self._json_schema = False
                        self._json_mode = False
                        kwargs.pop("response_format", None)
                        changed = True
                if "reasoning_effort" in kwargs and _REASONING_EFFORT_REJECTION.search(rejection):
                    self._reasoning_effort = None
                    kwargs.pop("reasoning_effort", None)
                    changed = True
                if not changed:
                    raise

    def _api(self) -> OpenAiLike:
        client = self._client
        if client is None:
            api_key = self._settings.api_key
            if api_key is None:
                raise LlmNotConfiguredError("LLM API key is missing")
            client = AsyncOpenAI(
                api_key=api_key.get_secret_value(),
                base_url=self._settings.base_url,
                timeout=self._settings.timeout_seconds,
                # Some gateways require their own routing header; it belongs to
                # the connection, not to each call.
                default_headers=self._settings.extra_headers or None,
                # Retries live in complete_json, where they are visible in the log.
                max_retries=0,
            )
            self._client = client
        return client


class OpenAiCompatibleClient:
    """A per-request view over an :class:`LlmTransport`.

    The client keeps the request-bound concerns — the audit sink, settings for
    this call — while the transport keeps the connection and the provider
    lessons. Without an explicit transport it builds its own, which is what
    tests and isolated callers want; such a client closes that transport in
    :meth:`aclose`. A client given a shared transport never closes it.

    Invariant: a client's settings must agree with its transport's settings on
    everything that reaches the wire (``model``, ``temperature``,
    ``max_output_tokens``, ``timeout_seconds`` and ``reasoning_effort``). The
    request parameters and the learned flags live on the transport, while
    :attr:`model_name` and the audit row are read from this client; differing
    settings would make the audit name a model the request never used.
    """

    def __init__(
        self,
        settings: LlmSettings,
        *,
        sink: LlmCallSink | None = None,
        transport: LlmTransport | None = None,
        client: OpenAiLike | None = None,
    ) -> None:
        if transport is not None and client is not None:
            raise ValueError("pass either a transport or a client, not both")
        self._settings = settings
        self._sink = sink
        self._owns_transport = transport is None
        self._transport = transport or LlmTransport(settings, client=client)

    @property
    def model_name(self) -> str:
        return self._settings.model

    @property
    def available(self) -> bool:
        return self._settings.is_configured

    async def aclose(self) -> None:
        """Close the connection only if this client created the transport."""
        if self._owns_transport:
            await self._transport.aclose()

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: type[ModelT],
        user_id: int | None = None,
    ) -> ModelT:
        """Ask the model for a JSON object matching ``schema``.

        The whole repair loop runs inside one deadline. The SDK's per-attempt
        timeout is not enough on its own: a hanging provider would otherwise
        hold the caller for ``retries × timeout``, and the user waits for the
        answer in a chat.
        """
        if not self.available:
            raise LlmNotConfiguredError("LLM is disabled or has no API key")

        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        attempts = self._settings.retries + 1
        last_error = "no attempt was made"
        attempt = 0
        started = time.perf_counter()
        # The paid answer is reported after the deadline block: a slow sink must
        # not turn a successful model call into a "deadline exceeded" failure.
        success: tuple[ModelT, str, Any, float] | None = None

        try:
            async with asyncio.timeout(self._settings.timeout_seconds):
                for attempt in range(1, attempts + 1):
                    attempt_started = time.perf_counter()
                    try:
                        response = await self._create(messages, schema=schema)
                    except _FATAL as exc:
                        error = _provider_error(exc)
                        await self._report(
                            purpose=purpose,
                            user_id=user_id,
                            attempt=attempt,
                            started=attempt_started,
                            success=False,
                            error=error,
                        )
                        raise LlmNotConfiguredError(error) from None
                    except (BadRequestError, APIStatusError, OpenAIError) as exc:
                        exhausted_quota = _is_exhausted_quota(exc)
                        last_error = _provider_error(exc, exhausted_quota=exhausted_quota)
                        await self._report(
                            purpose=purpose,
                            user_id=user_id,
                            attempt=attempt,
                            started=attempt_started,
                            success=False,
                            error=last_error,
                        )
                        if exhausted_quota:
                            # Fail fast: the plan is spent, retrying the same key
                            # cannot help. The failover route gets its turn.
                            raise LlmResponseError(last_error) from None
                        if isinstance(exc, _RETRIABLE) and attempt < attempts:
                            continue
                        if (
                            isinstance(exc, APIStatusError)
                            and 500 <= exc.status_code < 600
                            and attempt < attempts
                        ):
                            continue
                        raise LlmResponseError(last_error) from None

                    choice = response.choices[0] if response.choices else None
                    content = (choice.message.content if choice else None) or ""
                    usage = response.usage

                    if choice is not None and choice.finish_reason == "length":
                        # The cap covers reasoning tokens too. A reasoning model can
                        # spend all of it thinking and return an empty or half-written
                        # answer; a repair prompt would only spend more of the same
                        # budget. Say what to raise instead of blaming the model.
                        last_error = (
                            "response was cut off: "
                            f"finish_reason=length, "
                            f"max_output_tokens={self._settings.max_output_tokens}; "
                            "raise LLM__MAX_OUTPUT_TOKENS or ask for fewer words"
                        )
                        await self._report(
                            purpose=purpose,
                            user_id=user_id,
                            attempt=attempt,
                            started=attempt_started,
                            success=False,
                            request_text=user,
                            response_text=content,
                            prompt_tokens=getattr(usage, "prompt_tokens", None),
                            completion_tokens=getattr(usage, "completion_tokens", None),
                            total_tokens=getattr(usage, "total_tokens", None),
                            error=last_error,
                        )
                        raise LlmResponseError(last_error)

                    try:
                        payload = json.loads(extract_json_object(content))
                        result = schema.model_validate(payload)
                    except (json.JSONDecodeError, ValidationError) as exc:
                        last_error = _invalid_response_error(exc)
                        await self._report(
                            purpose=purpose,
                            user_id=user_id,
                            attempt=attempt,
                            started=attempt_started,
                            success=False,
                            request_text=user,
                            response_text=content,
                            prompt_tokens=getattr(usage, "prompt_tokens", None),
                            completion_tokens=getattr(usage, "completion_tokens", None),
                            total_tokens=getattr(usage, "total_tokens", None),
                            error=last_error,
                        )
                        if attempt >= attempts:
                            break
                        # Show the model its own answer and the exact complaint.
                        messages = [
                            *messages,
                            {"role": "assistant", "content": _echo_for_repair(content)},
                            {
                                "role": "user",
                                "content": REPAIR_TEMPLATE.format(error=last_error),
                            },
                        ]
                        continue

                    success = (result, content, usage, attempt_started)
                    break
        except TimeoutError as exc:
            last_error = (
                f"LLM call deadline of {self._settings.timeout_seconds}s exceeded "
                f"after {max(attempt, 1)} attempt(s)"
            )
            # ``started`` is the beginning of the whole call, so the latency in
            # this row is the full wall time, not the last attempt's slice.
            await self._report(
                purpose=purpose,
                user_id=user_id,
                attempt=max(attempt, 1),
                started=started,
                success=False,
                error=last_error,
            )
            raise LlmResponseError(last_error) from exc

        if success is not None:
            result, content, usage, attempt_started = success
            await self._report(
                purpose=purpose,
                user_id=user_id,
                attempt=attempt,
                started=attempt_started,
                success=True,
                request_text=user,
                response_text=content,
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
                total_tokens=getattr(usage, "total_tokens", None),
            )
            return result

        raise LlmResponseError(
            f"model did not return valid JSON after {attempts} attempts: {last_error}"
        )

    async def _create(
        self,
        messages: list[ChatCompletionMessageParam],
        *,
        schema: type[BaseModel],
    ) -> Any:
        """Let the shared transport make the call and learn from rejections."""
        return await self._transport.create(messages, schema=schema)

    async def _report(
        self,
        *,
        purpose: str,
        attempt: int,
        started: float,
        success: bool,
        user_id: int | None = None,
        request_text: str | None = None,
        response_text: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        error: str | None = None,
    ) -> None:
        if self._sink is None:
            return
        await self._sink(
            LlmCallRecord(
                purpose=purpose,
                provider_model=self._settings.model,
                success=success,
                user_id=user_id,
                attempt=attempt,
                request_text=request_text,
                response_text=response_text,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=error,
            )
        )


class FailoverClient:
    """One logical model route with a standby behind it.

    The primary is tried first. A model-layer failure that belongs to the
    provider — a dead key, an exhausted monthly quota, an outage, an unusable
    answer — switches to the standby for the same request. The bot's own
    refusals (:class:`LlmQuotaExceededError`, :class:`LlmGlobalBudgetExceededError`)
    are policy, not provider health, and are never bypassed by the standby.
    """

    def __init__(
        self,
        primary: JsonChatClient | None,
        fallback: JsonChatClient | None,
    ) -> None:
        if primary is None and fallback is None:
            raise ValueError("a failover client needs at least one route")
        self._primary = primary
        self._fallback = fallback

    @property
    def model_name(self) -> str:
        route = self._primary if self._primary is not None else self._fallback
        assert route is not None
        return route.model_name

    @property
    def available(self) -> bool:
        return (self._primary is not None and self._primary.available) or (
            self._fallback is not None and self._fallback.available
        )

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
        async def run(route: JsonChatClient) -> tuple[ModelT, str]:
            return await complete_json_with_model(
                route,
                purpose=purpose,
                system=system,
                user=user,
                schema=schema,
                user_id=user_id,
            )

        primary = self._primary
        if primary is None:
            assert self._fallback is not None
            return await run(self._fallback)
        try:
            return await run(primary)
        except (LlmQuotaExceededError, LlmGlobalBudgetExceededError):
            raise
        except LlmError as exc:
            if self._fallback is None:
                raise
            log.warning(
                "llm_failover_used",
                primary_model=primary.model_name,
                fallback_model=self._fallback.model_name,
                error=f"{type(exc).__name__}: {exc}",
            )
            return await run(self._fallback)
