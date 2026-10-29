"""Translating bare terms during a CSV import."""

from __future__ import annotations

import pytest

from eng_remember.errors import LlmNotConfiguredError
from eng_remember.services.llm.prompts import (
    TRANSLATION_SYSTEM_PROMPT,
    build_translation_prompt,
)
from eng_remember.services.llm.translator import TermTranslator
from tests.factories import ScriptedClient


async def test_translate_returns_a_mapping_and_is_attributed() -> None:
    client = ScriptedClient()
    client.push_translations(
        {
            "translations": [
                {"term": "run", "translation": "бежать"},
                {"term": "swim", "translation": "плавать"},
            ]
        }
    )
    translator = TermTranslator(llm=client)

    result = await translator.translate(terms=["run", "swim"], user_id=7)

    assert result == {"run": "бежать", "swim": "плавать"}
    assert client.calls[-1]["purpose"] == "translate_terms"
    assert client.calls[-1]["user_id"] == 7


async def test_translation_uses_the_requested_target_language() -> None:
    client = ScriptedClient()
    client.push_translations({"translations": [{"term": "bonjour", "translation": "здравствуйте"}]})
    translator = TermTranslator(llm=client)

    await translator.translate(terms=["bonjour"], language="fra", user_id=7)

    assert "Target language: French (fra)." in client.calls[-1]["user"]
    assert "English" not in TRANSLATION_SYSTEM_PROMPT


async def test_duplicate_and_blank_entries_are_dropped() -> None:
    client = ScriptedClient()
    client.push_translations(
        {
            "translations": [
                {"term": "Run", "translation": "бежать"},
                {"term": "run", "translation": "бежать"},
                {"term": " ", "translation": "x"},
                {"term": "swim", "translation": ""},
            ]
        }
    )
    translator = TermTranslator(llm=client)

    result = await translator.translate(terms=["run", "swim"], user_id=1)

    assert result == {"run": "бежать"}


async def test_translator_refuses_without_a_model() -> None:
    translator = TermTranslator(llm=None)

    assert translator.available is False
    with pytest.raises(LlmNotConfiguredError):
        await translator.translate(terms=["run"])


def test_the_prompt_wraps_terms_and_strips_brackets() -> None:
    prompt = build_translation_prompt(["run", "<b>swim</b>", "a" * 300])

    assert "<terms>" in prompt
    assert "<b>" not in prompt
    assert "a" * 300 not in prompt
