"""Writing fresh examples and mnemonics for cards that already exist.

The fourth LLM call in the system, and a different job from parsing and
expansion: nothing here proposes new vocabulary. The card is already saved and
the model only adds context to it — a couple more sentences to rotate through,
or a hook to remember it by.

Both answers are content for one card, so there is no batch to filter — the
caller decides whether to keep what came back.
"""

from __future__ import annotations

from collections.abc import Sequence

from eng_remember.domain.languages import DEFAULT_LANGUAGE
from eng_remember.errors import LlmNotConfiguredError
from eng_remember.services.llm.client import JsonChatClient
from eng_remember.services.llm.prompts import (
    ENRICH_EXAMPLES_SYSTEM_PROMPT,
    MNEMONIC_SYSTEM_PROMPT,
    build_enrich_examples_prompt,
    build_mnemonic_prompt,
)
from eng_remember.services.llm.schema import ExamplesResponse, MnemonicResponse

__all__ = ["CardEnricher"]

PURPOSE_EXAMPLES = "card_examples"
PURPOSE_MNEMONIC = "mnemonic"


class CardEnricher:
    def __init__(self, llm: JsonChatClient | None = None) -> None:
        self._llm = llm

    @property
    def available(self) -> bool:
        return self._llm is not None and self._llm.available

    async def examples(
        self,
        *,
        term: str,
        translation: str,
        count: int,
        existing: Sequence[str],
        user_id: int | None = None,
        language: str = DEFAULT_LANGUAGE,
    ) -> list[str]:
        """Fresh sentences for the card. Raises :class:`LlmNotConfiguredError`."""
        llm = self._require_client()
        response = await llm.complete_json(
            purpose=PURPOSE_EXAMPLES,
            system=ENRICH_EXAMPLES_SYSTEM_PROMPT,
            user=build_enrich_examples_prompt(
                term=term,
                translation=translation,
                count=count,
                existing=list(existing),
                language=language,
            ),
            schema=ExamplesResponse,
            user_id=user_id,
        )
        return response.examples

    async def mnemonic(
        self,
        *,
        term: str,
        translation: str,
        user_id: int | None = None,
        language: str = DEFAULT_LANGUAGE,
    ) -> str:
        """A memory hook for the card. Raises :class:`LlmNotConfiguredError`."""
        llm = self._require_client()
        response = await llm.complete_json(
            purpose=PURPOSE_MNEMONIC,
            system=MNEMONIC_SYSTEM_PROMPT,
            user=build_mnemonic_prompt(term=term, translation=translation, language=language),
            schema=MnemonicResponse,
            user_id=user_id,
        )
        return response.mnemonic

    def _require_client(self) -> JsonChatClient:
        llm = self._llm
        if llm is None or not llm.available:
            raise LlmNotConfiguredError("card enrichment needs a configured LLM")
        return llm
