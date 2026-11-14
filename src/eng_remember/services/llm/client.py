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
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar, runtime_checkable

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
from eng_remember.errors import LlmNotConfiguredError, LlmResponseError
from eng_remember.repositories.llm_calls import LlmCallRecord
from eng_remember.services.llm.prompts import REPAIR_TEMPLATE
from eng_remember.services.llm.schema import extract_json_object

__all__ = [
    "JsonChatClient",
    "LlmCallSink",
    "LlmTransport",
    "OpenAiCompatibleClient",
    "OpenAiLike",
]

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


def _rejection_text(exc: BadRequestError) -> str:
    """Everything the provider said about a rejection, lowercased.

    The human message is not the only place a parameter can be named: OpenAI
    and compatible providers often put a machine-readable ``param`` or ``code``
    in the structured body while the message stays generic.
    """
    parts = [str(exc)]
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
    return " ".join(parts).lower()


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


class LlmTransport:
    """The process-wide HTTP side of the model: one client, one set of lessons.

    Created once at startup and shared by every request, so the (lazily built)
    ``AsyncOpenAI`` connection pool is reused instead of leaking one client per
    update. It also owns the flags learned from provider rejections: once a
    provider says it does not understand ``response_format`` or
    ``reasoning_effort``, every later request remembers that instead of paying
    for the same 400 again.

    Tests can pass a ready-made ``client``; production leaves it ``None`` and the
    real SDK is built on first use.
    """

    def __init__(self, settings: LlmSettings, *, client: OpenAiLike | None = None) -> None:
        self._settings = settings
        self._client = client
        # Flipped off for the lifetime of the transport the first time a
        # provider rejects ``response_format``; small self-hosted servers often do.
        self._json_mode = True
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
        if self._json_mode:
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
                if isinstance(response_format, dict) and _RESPONSE_FORMAT_REJECTION.search(
                    rejection
                ):
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

        try:
            async with asyncio.timeout(self._settings.timeout_seconds):
                for attempt in range(1, attempts + 1):
                    attempt_started = time.perf_counter()
                    try:
                        response = await self._create(messages)
                    except _FATAL as exc:
                        await self._report(
                            purpose=purpose,
                            user_id=user_id,
                            attempt=attempt,
                            started=attempt_started,
                            success=False,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        raise LlmNotConfiguredError(str(exc)) from exc
                    except (BadRequestError, APIStatusError, OpenAIError) as exc:
                        await self._report(
                            purpose=purpose,
                            user_id=user_id,
                            attempt=attempt,
                            started=attempt_started,
                            success=False,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        last_error = f"{type(exc).__name__}: {exc}"
                        if isinstance(exc, _RETRIABLE) and attempt < attempts:
                            continue
                        if (
                            isinstance(exc, APIStatusError)
                            and 500 <= exc.status_code < 600
                            and attempt < attempts
                        ):
                            continue
                        raise LlmResponseError(last_error) from exc

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
                        last_error = f"{type(exc).__name__}: {exc}"
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
        except TimeoutError as exc:
            last_error = (
                f"LLM call deadline of {self._settings.timeout_seconds}s exceeded "
                f"after {max(attempt, 1)} attempt(s)"
            )
            await self._report(
                purpose=purpose,
                user_id=user_id,
                attempt=max(attempt, 1),
                started=started,
                success=False,
                error=last_error,
            )
            raise LlmResponseError(last_error) from exc

        raise LlmResponseError(
            f"model did not return valid JSON after {attempts} attempts: {last_error}"
        )

    async def _create(self, messages: list[ChatCompletionMessageParam]) -> Any:
        """Let the shared transport make the call and learn from rejections."""
        return await self._transport.create(messages)

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
