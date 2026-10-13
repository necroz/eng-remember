"""The rule-based parser that keeps the bot usable without a model."""

from __future__ import annotations

import pytest

from eng_remember.errors import ParseError
from eng_remember.services.llm.fallback import FallbackParser, parse_text
from eng_remember.services.llm.schema import Intent


@pytest.mark.parametrize(
    ("text", "term", "translation"),
    [
        ("ubiquitous - вездесущий", "ubiquitous", "вездесущий"),
        ("ubiquitous — вездесущий", "ubiquitous", "вездесущий"),
        ("ubiquitous – вездесущий", "ubiquitous", "вездесущий"),
        ("ubiquitous: вездесущий", "ubiquitous", "вездесущий"),
        ("ubiquitous = вездесущий", "ubiquitous", "вездесущий"),
        ("ubiquitous -> вездесущий", "ubiquitous", "вездесущий"),
        ("ubiquitous → вездесущий", "ubiquitous", "вездесущий"),
        ("to run - бежать", "to run", "бежать"),
        ("a piece of cake - проще простого", "a piece of cake", "проще простого"),
    ],
)
def test_separator_formats(text: str, term: str, translation: str) -> None:
    result = parse_text(text)

    assert result.intent is Intent.ADD_CARDS
    assert len(result.cards) == 1
    assert result.cards[0].term == term
    assert result.cards[0].translation == translation


def test_several_lines_become_several_cards() -> None:
    result = parse_text("resilient - устойчивый\nthorough - тщательный\nubiquitous - вездесущий")

    assert [card.term for card in result.cards] == ["resilient", "thorough", "ubiquitous"]


def test_semicolons_and_leading_verb_are_ignored() -> None:
    result = parse_text("добавь слова: run - бежать; jump - прыгать")

    assert [card.term for card in result.cards] == ["run", "jump"]


def test_transcription_in_brackets_is_extracted() -> None:
    card = parse_text("ubiquitous [juːˈbɪkwɪtəs] — вездесущий").cards[0]

    assert card.term == "ubiquitous"
    assert card.transcription == "/juːˈbɪkwɪtəs/"


def test_transcription_with_slashes_in_parentheses() -> None:
    card = parse_text("resilient (/rɪˈzɪliənt/) - устойчивый").cards[0]

    assert card.term == "resilient"
    assert card.transcription == "/rɪˈzɪliənt/"


def test_bare_ipa_is_extracted() -> None:
    card = parse_text("thorough /ˈθʌrə/ — тщательный").cards[0]

    assert card.term == "thorough"
    assert card.transcription == "/ˈθʌrə/"


def test_bare_word_without_a_translation_is_rejected() -> None:
    """A lone word still becomes a card; the model would fill the meaning in."""
    with pytest.raises(ParseError):
        parse_text("ubiquitous")


@pytest.mark.parametrize(
    "text",
    [
        "привет, как дела?",
        "вездесущий — ubiquitous",
        "",
        "   ",
        "42 - 43",
    ],
)
def test_non_english_input_is_rejected(text: str) -> None:
    with pytest.raises(ParseError):
        parse_text(text)


def test_wrapped_dash_does_not_split_hyphenated_words() -> None:
    """``well-known`` must survive; ``a - b`` must split."""
    result = parse_text("well-known - широко известный")

    assert result.cards[0].term == "well-known"
    assert result.cards[0].translation == "широко известный"


async def test_object_form_matches_the_text_api() -> None:
    parser = FallbackParser()

    assert parser.available
    outcome = await parser.parse("ubiquitous - вездесущий", user_id=1)

    assert outcome.cards[0].term == "ubiquitous"
