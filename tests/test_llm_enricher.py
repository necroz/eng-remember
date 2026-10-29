"""The enricher component: examples and mnemonics for an existing card."""

from __future__ import annotations

import pytest

from eng_remember.errors import LlmNotConfiguredError
from eng_remember.services.llm.enricher import CardEnricher
from eng_remember.services.llm.prompts import (
    ENRICH_EXAMPLES_SYSTEM_PROMPT,
    build_enrich_examples_prompt,
    build_mnemonic_prompt,
)
from tests.factories import ScriptedClient


async def test_examples_come_back_and_are_attributed() -> None:
    client = ScriptedClient()
    client.push_examples({"examples": ["He runs fast.", "A quick run."]})
    enricher = CardEnricher(llm=client)

    result = await enricher.examples(
        term="run", translation="бежать", count=2, existing=["He runs."], user_id=7
    )

    assert result == ["He runs fast.", "A quick run."]
    assert client.calls[-1]["purpose"] == "card_examples"
    assert client.calls[-1]["user_id"] == 7


async def test_examples_use_the_requested_target_language() -> None:
    client = ScriptedClient()
    client.push_examples({"examples": ["Bonjour, ça va ?"]})
    enricher = CardEnricher(llm=client)

    await enricher.examples(
        term="bonjour",
        translation="здравствуйте",
        count=1,
        existing=[],
        language="fra",
        user_id=7,
    )

    assert "Target language: French (fra)." in client.calls[-1]["user"]
    assert "English" not in ENRICH_EXAMPLES_SYSTEM_PROMPT


async def test_mnemonic_uses_the_requested_target_language() -> None:
    client = ScriptedClient()
    client.push_mnemonic({"mnemonic": "«Бонжур» — как «бонус»."})
    enricher = CardEnricher(llm=client)

    await enricher.mnemonic(term="bonjour", translation="здравствуйте", language="fra", user_id=7)

    assert "Target language: French (fra)." in client.calls[-1]["user"]


async def test_mnemonic_comes_back_and_is_attributed() -> None:
    client = ScriptedClient()
    client.push_mnemonic({"mnemonic": "«Ран» — он бежит рано."})
    enricher = CardEnricher(llm=client)

    mnemonic = await enricher.mnemonic(term="run", translation="бежать", user_id=7)

    assert mnemonic == "«Ран» — он бежит рано."
    assert client.calls[-1]["purpose"] == "mnemonic"
    assert client.calls[-1]["user_id"] == 7


async def test_the_enricher_refuses_without_a_model() -> None:
    enricher = CardEnricher(llm=None)

    assert enricher.available is False
    with pytest.raises(LlmNotConfiguredError):
        await enricher.examples(term="run", translation="бежать", count=1, existing=[])


def test_the_enrich_prompt_wraps_untrusted_values() -> None:
    prompt = build_enrich_examples_prompt(
        term="run", translation="бежать", count=2, existing=["<b>bold</b>" * 50]
    )

    assert "<term>run</term>" in prompt
    assert "<b>" not in prompt


def test_the_mnemonic_prompt_wraps_untrusted_values() -> None:
    prompt = build_mnemonic_prompt(term="run", translation="бежать <script>")

    assert "<term>run</term>" in prompt
    assert "<script>" not in prompt
