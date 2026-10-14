"""Translating bare terms, for vocabulary arriving as a file.

Imports often carry words without meanings — a spreadsheet of terms, an export
from another tool. This is the fifth LLM call in the system: it only gives a
Russian meaning to terms that already exist, and the caller decides how many it
is willing to pay for.
"""

from __future__ import annotations

from collections.abc import Sequence

from eng_remember.errors import LlmNotConfiguredError
from eng_remember.services.llm.client import JsonChatClient
from eng_remember.services.llm.prompts import (
    TRANSLATION_SYSTEM_PROMPT,
    build_translation_prompt,
)
from eng_remember.services.llm.schema import TranslationsResponse

__all__ = ["TermTranslator"]

PURPOSE_TRANSLATE = "translate_terms"


class TermTranslator:
    def __init__(self, llm: JsonChatClient | None = None) -> None:
        self._llm = llm

    @property
    def available(self) -> bool:
        return self._llm is not None and self._llm.available

    async def translate(
        self,
        *,
        terms: Sequence[str],
        user_id: int | None = None,
    ) -> dict[str, str]:
        """Term (lower-cased) to Russian meaning. Raises without a model."""
        llm = self._llm
        if llm is None or not llm.available:
            raise LlmNotConfiguredError("translation needs a configured LLM")

        response = await llm.complete_json(
            purpose=PURPOSE_TRANSLATE,
            system=TRANSLATION_SYSTEM_PROMPT,
            user=build_translation_prompt(list(terms)),
            schema=TranslationsResponse,
            user_id=user_id,
        )

        mapping: dict[str, str] = {}
        for entry in response.translations:
            mapping.setdefault(entry.term.casefold(), entry.translation)
        return mapping
