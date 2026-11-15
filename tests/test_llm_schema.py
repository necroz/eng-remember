"""Validation rules for what the model hands back."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from eng_remember.services.llm.schema import (
    PART_OF_SPEECH_LIMIT,
    ExamplesResponse,
    ExpansionResponse,
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


def test_part_of_speech_limit_matches_column() -> None:
    """The schema must never hand the 32-character column a longer value."""
    card = ParsedCard(term="run", translation="бежать", part_of_speech="a" * 40)

    assert PART_OF_SPEECH_LIMIT == 32
    assert card.part_of_speech == "a" * 32


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


def test_answer_is_capped() -> None:
    response = ParseResponse.model_validate({"answer": "я" * 5000})

    assert response.answer is not None
    assert len(response.answer) == 2000


def test_answer_keeps_line_breaks_under_the_cap() -> None:
    """An explanation is prose: only its ceiling matters, not its shape."""
    response = ParseResponse.model_validate({"answer": "Первая строка.\nВторая строка."})

    assert response.answer == "Первая строка.\nВторая строка."


def test_answer_drops_control_characters_but_keeps_newlines() -> None:
    """A stored explanation must not smuggle terminal control codes to Telegram."""
    response = ParseResponse.model_validate({"answer": "Первая\x00строка.\nВторая\x07строка.\x1b"})

    assert response.answer == "Первая строка.\nВторая строка."


def test_expansion_cards_are_capped_like_parse_cards() -> None:
    """Both card lists share one ceiling, so neither can flood the database."""
    response = ExpansionResponse.model_validate(
        {"cards": [{"term": f"word{i}", "translation": f"слово{i}"} for i in range(200)]}
    )

    assert len(response.cards) == 50
    assert response.cards[-1].term == "word49"


def test_cards_list_is_capped() -> None:
    response = ParseResponse.model_validate(
        {"cards": [{"term": f"word{i}", "translation": f"слово{i}"} for i in range(200)]}
    )

    assert len(response.cards) == 50
    assert response.cards[0].term == "word0"
    assert response.cards[-1].term == "word49"


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


def test_json_is_recovered_from_a_balanced_object_after_prose_braces() -> None:
    """Prose braces and two objects: take the first candidate that really parses.

    The scanner must respect nesting, quoted strings and escapes — the ``}``
    inside the string is not the end of the object.
    """
    raw = 'Use {braces} in prose: {"a": {"b": 2}, "c": "}"} then {"d": 3}'

    assert extract_json_object(raw) == '{"a": {"b": 2}, "c": "}"}'


def test_json_is_recovered_across_escaped_quotes() -> None:
    """An escaped quote inside a string must not close the enclosing quote."""
    raw = r'Here: {"a": "he said \"hi\"", "b": 1} end'

    assert extract_json_object(raw) == r'{"a": "he said \"hi\"", "b": 1}'


def test_json_is_recovered_after_an_unclosed_prose_brace() -> None:
    """An unbalanced prose brace before the object does not hide it."""
    raw = 'prose { not closed {"a": 1}'

    assert extract_json_object(raw) == '{"a": 1}'


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
