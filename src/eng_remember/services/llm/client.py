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

import json
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

__all__ = ["JsonChatClient", "LlmCallSink", "OpenAiCompatibleClient", "OpenAiLike"]

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


class OpenAiCompatibleClient:
    def __init__(
        self,
        settings: LlmSettings,
        *,
        sink: LlmCallSink | None = None,
        client: OpenAiLike | None = None,
    ) -> None:
        self._settings = settings
        self._sink = sink
        self._client = client
        # Flipped off for the lifetime of the client the first time a provider
        # rejects ``response_format``; small self-hosted servers often do.
        self._json_mode = True

    @property
    def model_name(self) -> str:
        return self._settings.model

    @property
    def available(self) -> bool:
        return self._settings.is_configured

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: type[ModelT],
        user_id: int | None = None,
    ) -> ModelT:
        """Ask the model for a JSON object matching ``schema``."""
        if not self.available:
            raise LlmNotConfiguredError("LLM is disabled or has no API key")

        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        attempts = self._settings.retries + 1
        last_error = "no attempt was made"

        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                response = await self._create(messages)
            except _FATAL as exc:
                await self._report(
                    purpose=purpose,
                    user_id=user_id,
                    attempt=attempt,
                    started=started,
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise LlmNotConfiguredError(str(exc)) from exc
            except (BadRequestError, APIStatusError, OpenAIError) as exc:
                await self._report(
                    purpose=purpose,
                    user_id=user_id,
                    attempt=attempt,
                    started=started,
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

            try:
                payload = json.loads(extract_json_object(content))
                result = schema.model_validate(payload)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await self._report(
                    purpose=purpose,
                    user_id=user_id,
                    attempt=attempt,
                    started=started,
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
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": REPAIR_TEMPLATE.format(error=last_error)},
                ]
                continue

            await self._report(
                purpose=purpose,
                user_id=user_id,
                attempt=attempt,
                started=started,
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

    async def _create(self, messages: list[ChatCompletionMessageParam]) -> Any:
        """Call the endpoint, degrading gracefully when JSON mode is unsupported."""
        api = self._api()
        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "messages": messages,
            "temperature": self._settings.temperature,
            "max_tokens": self._settings.max_output_tokens,
        }
        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            return await api.chat.completions.create(**kwargs)
        except BadRequestError as exc:
            if self._json_mode and "response_format" in str(exc):
                self._json_mode = False
                kwargs.pop("response_format", None)
                return await api.chat.completions.create(**kwargs)
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
