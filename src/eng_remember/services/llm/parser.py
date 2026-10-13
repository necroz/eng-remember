"""Turn a free-form message into structured cards.

Sits between the bot and the model: it decides whether a model call happens at
all, degrades to the rule-based parser when the model is unavailable or fails,
and assembles the audit sink that writes every attempt into ``llm_calls``.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.config import Settings
from eng_remember.errors import LlmError, ParseError
from eng_remember.services.llm.client import JsonChatClient
from eng_remember.services.llm.factory import build_llm_client
from eng_remember.services.llm.fallback import FallbackParser
from eng_remember.services.llm.prompts import (
    PARSE_SYSTEM_PROMPT,
    build_clarified_prompt,
    build_user_prompt,
)
from eng_remember.services.llm.schema import ParseResponse

__all__ = ["MessageParser", "ParseOutcome", "build_message_parser"]

log = structlog.get_logger(__name__)

PURPOSE_PARSE = "parse_message"


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """Result of parsing plus which engine produced it."""

    response: ParseResponse
    engine: str

    @property
    def used_llm(self) -> bool:
        return self.engine != "rules"


class MessageParser:
    def __init__(
        self,
        *,
        llm: JsonChatClient | None = None,
        fallback: FallbackParser | None = None,
    ) -> None:
        self._llm = llm
        self._fallback = fallback or FallbackParser()

    @property
    def engine(self) -> str:
        if self._llm is not None and self._llm.available:
            return self._llm.model_name
        return self._fallback.name

    async def parse(self, text: str, *, user_id: int | None = None) -> ParseOutcome:
        """Parse ``text``, preferring the model and falling back to rules."""
        cleaned = text.strip()
        if not cleaned:
            raise ParseError("empty message")
        return await self._parse(build_user_prompt(cleaned), cleaned, user_id=user_id)

    async def parse_clarified(
        self,
        *,
        original: str,
        question: str,
        answer: str,
        user_id: int | None = None,
    ) -> ParseOutcome:
        """Parse the original request again, now that one detail is known."""
        if not (original.strip() and question.strip()):
            raise ParseError("nothing to clarify")
        prompt = build_clarified_prompt(original, question, answer)
        # The rule-based fallback cannot use the clarification context, so it
        # re-reads the original text — which is the honest degradation.
        return await self._parse(prompt, original, user_id=user_id)

    async def _parse(
        self,
        prompt: str,
        fallback_text: str,
        *,
        user_id: int | None,
    ) -> ParseOutcome:
        llm = self._llm
        if llm is not None and llm.available:
            try:
                response = await llm.complete_json(
                    purpose=PURPOSE_PARSE,
                    system=PARSE_SYSTEM_PROMPT,
                    user=prompt,
                    schema=ParseResponse,
                    user_id=user_id,
                )
                return ParseOutcome(response=response, engine=llm.model_name)
            except LlmError as exc:
                log.warning("llm_parse_failed", error=str(exc), fallback=self._fallback.name)

        response = await self._fallback.parse(fallback_text, user_id=user_id)
        return ParseOutcome(response=response, engine=self._fallback.name)


def build_message_parser(session: AsyncSession, settings: Settings) -> MessageParser:
    """Assemble a parser bound to one request's database session."""
    return MessageParser(llm=build_llm_client(session, settings))
