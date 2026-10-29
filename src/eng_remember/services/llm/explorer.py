"""Finding new words for a topic, or inside a text the learner pasted.

The second LLM call in the system, and a different job from parsing: the parser
reads what the user wrote, the explorer produces words that were not in the
message at all. It knows nothing about databases — de-duplication, the
collection check and the text check live in
:mod:`eng_remember.services.discovery`.
"""

from __future__ import annotations

import structlog

from eng_remember.domain.languages import DEFAULT_LANGUAGE, resolve_target_language
from eng_remember.errors import LlmNotConfiguredError
from eng_remember.services.llm.client import JsonChatClient
from eng_remember.services.llm.prompts import EXPANSION_SYSTEM_PROMPT, build_expansion_prompt
from eng_remember.services.llm.schema import ExpansionResponse

__all__ = ["WordExplorer"]

log = structlog.get_logger(__name__)

PURPOSE_EXPAND = "expand_words"

# How many existing terms to name in the prompt. The learner's whole collection
# would swamp the instructions and cost tokens for little gain — duplicates are
# filtered in code regardless.
KNOWN_TERMS_IN_PROMPT = 40


class WordExplorer:
    def __init__(self, llm: JsonChatClient | None = None) -> None:
        self._llm = llm

    @property
    def available(self) -> bool:
        return self._llm is not None and self._llm.available

    @property
    def engine(self) -> str:
        return self._llm.model_name if self._llm is not None else "none"

    async def expand(
        self,
        *,
        count: int,
        topic: str | None = None,
        context_text: str | None = None,
        seeds: list[str] | None = None,
        known: list[str] | None = None,
        level: str | None = None,
        language: str = DEFAULT_LANGUAGE,
        user_id: int | None = None,
    ) -> ExpansionResponse:
        """Ask for words. Raises :class:`LlmNotConfiguredError` without a model.

        Refusing instead of returning an empty list matters: the caller has to
        tell the user *why* nothing was found, and "no model is configured" is a
        different message from "no words matched".
        """
        llm = self._llm
        if llm is None or not llm.available:
            raise LlmNotConfiguredError("word discovery needs a configured LLM")

        response = await llm.complete_json(
            purpose=PURPOSE_EXPAND,
            system=EXPANSION_SYSTEM_PROMPT,
            user=build_expansion_prompt(
                topic=topic,
                context_text=context_text,
                seeds=list(seeds or []),
                count=count,
                known=list(known or [])[:KNOWN_TERMS_IN_PROMPT],
                level=level,
                language=language,
            ),
            schema=ExpansionResponse,
            user_id=user_id,
        )
        target = resolve_target_language(language)
        for card in response.cards:
            card.language = target
        return response
