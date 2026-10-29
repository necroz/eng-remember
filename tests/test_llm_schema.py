"""Validation rules for what the model hands back."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from eng_remember.services.llm.schema import (
    ExamplesResponse,
    Intent,
    MnemonicResponse,
    ParsedCard,
    ParseResponse,
    extract_json_object,
)


def test_transcription_is_normalised_to_slashes() -> None:
    assert ParsedCard(term="a", translation="б", transcription="ˈæb").transcription == "/ˈæb/"
    assert ParsedCard(term="a", translation="б", transcription="[ˈæb]").transcription == "/ˈæb/"
    assert ParsedCard(term="a", translation="б", transcription="/ˈæb/").transcription == "/ˈæb/"
    assert ParsedCard(term="a", translation="б", transcription="  ").transcription is None


def test_part_of_speech_is_lowercased() -> None:
    assert ParsedCard(term="a", translation="б", part_of_speech="Noun").part_of_speech == "noun"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("fr", "fra"),
        ("zh-Hant", "cmn"),
        ("pt-BR", "por"),
        ("ar-EG", "arb"),
        ("ru", "eng"),
        ("unknown", "eng"),
        ("FR", "fra"),
    ],
)
def test_card_language_is_normalised_to_a_target_code(raw: str, expected: str) -> None:
    card = ParsedCard(term="bonjour", translation="здравствуйте", language=raw)

    assert card.language == expected


def test_card_language_reaches_the_draft_without_touching_the_translation() -> None:
    card = ParsedCard(term="bonjour", translation="здравствуйте", language="fr")

    assert card.to_draft().language == "fra"
    assert card.translation == "здравствуйте"


def test_parse_response_takes_its_language_from_the_first_card() -> None:
    response = ParseResponse.model_validate(
        {
            "cards": [
                {"term": "bonjour", "translation": "здравствуйте", "language": "fr"},
                {"term": "书", "translation": "книга", "language": "zh"},
            ]
        }
    )

    assert response.language == "fra"
    assert [card.language for card in response.cards] == ["fra", "cmn"]


def test_explicit_parse_language_beats_the_first_card() -> None:
    response = ParseResponse.model_validate(
        {
            "language": "eng",
            "cards": [{"term": "bonjour", "translation": "здравствуйте", "language": "fr"}],
        }
    )

    assert response.language == "eng"


def test_an_empty_parse_response_is_english() -> None:
    assert ParseResponse().language == "eng"
    assert ParseResponse.model_validate({"cards": []}).language == "eng"


def test_an_explicit_null_language_stays_english() -> None:
    response = ParseResponse.model_validate(
        {
            "language": None,
            "cards": [{"term": "bonjour", "translation": "здравствуйте", "language": "fr"}],
        }
    )

    assert response.language == "eng"


def test_blank_optional_fields_become_none() -> None:
    card = ParsedCard(term="run", translation="бежать", example="   ", notes="")

    assert card.example is None
    assert card.notes is None
    draft = card.to_draft()
    assert draft.example is None


def test_tags_accept_a_comma_separated_string() -> None:
    card = ParsedCard.model_validate({"term": "a", "translation": "б", "tags": "business, it"})

    assert card.tags == ["business", "it"]


def test_cards_without_a_translation_are_dropped() -> None:
    response = ParseResponse.model_validate(
        {
            "intent": "add_cards",
            "cards": [
                {"term": "run", "translation": "бежать"},
                {"term": "jump", "translation": "   "},
                {"term": "", "translation": "пусто"},
            ],
        }
    )

    assert [card.term for card in response.cards] == ["run"]


def test_drafts_are_trimmed_and_tag_cleaned() -> None:
    response = ParseResponse.model_validate(
        {
            "intent": "add_cards",
            "cards": [{"term": "  run  ", "translation": " бежать ", "tags": [" it ", ""]}],
        }
    )

    draft = response.to_drafts()[0]
    assert draft.term == "run"
    assert draft.translation == "бежать"
    assert draft.tags == ("it",)


def test_unknown_intent_and_missing_cards_are_tolerated() -> None:
    response = ParseResponse.model_validate({"intent": "greeting"})

    assert response.intent is Intent.UNKNOWN
    assert response.cards == []


def test_more_words_is_read_and_off_by_default() -> None:
    """The flag that keeps "add this word and more like it" from losing half."""
    assert ParseResponse().more_words is False
    response = ParseResponse.model_validate({"cards": [], "more_words": True})
    assert response.more_words is True


def test_a_models_own_name_for_the_mixed_request_means_both_halves() -> None:
    """ "add_and_expand" is not our vocabulary, but dropping the search half
    because of the label is exactly the failure the flag prevents."""
    response = ParseResponse.model_validate({"intent": "add_and_expand", "cards": []})

    assert response.intent is Intent.ADD_CARDS
    assert response.more_words is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"intent": "add_cards"}', '{"intent": "add_cards"}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('Вот результат:\n{"a": 1}\nГотово.', '{"a": 1}'),
        ('  {"a": {"b": 2}}  ', '{"a": {"b": 2}}'),
    ],
)
def test_json_is_recovered_from_chatty_answers(raw: str, expected: str) -> None:
    assert extract_json_object(raw) == expected


@pytest.mark.parametrize("raw", ["", "просто текст", "{не json}"])
def test_unusable_answers_raise(raw: str) -> None:
    with pytest.raises(json.JSONDecodeError):
        json.loads(extract_json_object(raw))


def test_parsed_card_merges_singular_and_plural_examples() -> None:
    singular = ParsedCard(term="run", translation="бежать", example="He runs.")

    assert singular.examples == ["He runs."]

    plural = ParsedCard.model_validate(
        {"term": "run", "translation": "бежать", "examples": "He runs.\nA run."}
    )

    assert plural.example == "He runs."
    assert plural.examples == ["He runs.", "A run."]
    assert plural.to_draft().examples == ("He runs.", "A run.")


def test_examples_response_dedupes_and_caps_at_five() -> None:
    response = ExamplesResponse.model_validate(
        {"examples": ["One.", " ", "one.", "Two.", "Three.", "Four.", "Five.", "Six."]}
    )

    assert response.examples == ["One.", "Two.", "Three.", "Four.", "Five."]


def test_mnemonic_response_rejects_blank() -> None:
    with pytest.raises(ValidationError):
        MnemonicResponse.model_validate({"mnemonic": "   "})


def test_cefr_is_normalised_and_malformed_values_become_none() -> None:
    assert ParsedCard(term="run", translation="бежать", cefr="b2").cefr == "B2"
    assert ParsedCard(term="run", translation="бежать", cefr="Z9").cefr is None
    assert ParsedCard(term="run", translation="бежать", cefr="B2/C1").cefr is None
    assert ParsedCard(term="run", translation="бежать", cefr="  ").cefr is None


def test_frequency_accepts_english_and_russian_names() -> None:
    assert ParsedCard(term="run", translation="бежать", frequency="common").frequency == "common"
    assert ParsedCard(term="run", translation="бежать", frequency="частое").frequency == "common"
    assert ParsedCard(term="run", translation="бежать", frequency="RARE").frequency == "rare"
    assert ParsedCard(term="run", translation="бежать", frequency="нечто").frequency is None


def test_draft_carries_the_level_fields() -> None:
    card = ParsedCard(term="run", translation="бежать", cefr="b1", frequency="Common")

    draft = card.to_draft()

    assert draft.cefr == "B1"
    assert draft.frequency == "common"
