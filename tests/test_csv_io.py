"""The CSV exchange format: what export writes and what import accepts."""

from __future__ import annotations

from datetime import UTC, datetime

from eng_remember.models.card import Card
from eng_remember.services.csv_io import (
    MAX_ROWS,
    cards_to_csv,
    parse_cards_csv,
)

HEADER = "term;translation;transcription;part_of_speech;example;notes;tags;deck"


def make_card(
    term: str,
    translation: str,
    *,
    transcription: str | None = None,
    part_of_speech: str | None = None,
    examples: list[str] | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
    deck_id: int = 1,
) -> Card:
    return Card(
        user_id=1,
        deck_id=deck_id,
        term=term,
        translation=translation,
        transcription=transcription,
        part_of_speech=part_of_speech,
        example=(examples or [None])[0],
        examples=examples or [],
        notes=notes,
        tags=tags or [],
        due_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_export_writes_a_header_and_rows() -> None:
    text = cards_to_csv(
        [
            make_card(
                "run",
                "бежать",
                transcription="/rʌn/",
                part_of_speech="verb",
                examples=["He runs."],
                tags=["sport", "basic"],
            ),
            make_card("house", "дом"),
        ],
        deck_names={1: "Основная колода"},
    )

    lines = text.splitlines()
    assert lines[0] == HEADER
    assert lines[1] == "run;бежать;/rʌn/;verb;He runs.;;sport|basic;Основная колода"
    assert lines[2] == "house;дом;;;;;;Основная колода"


def test_export_escapes_delimiters_and_roundtrips() -> None:
    card = make_card("get away with", "выйти сухим из воды; избежать наказания", notes="разг.")

    parsed = parse_cards_csv(cards_to_csv([card]))

    assert len(parsed.rows) == 1
    row = parsed.rows[0]
    assert row.term == "get away with"
    assert row.translation == "выйти сухим из воды; избежать наказания"
    assert row.notes == "разг."


def test_parse_accepts_comma_and_tab_delimiters() -> None:
    comma = parse_cards_csv("run,бежать\nswim,плавать\n")
    tab = parse_cards_csv("run\tбежать\nswim\tплавать\n")

    assert [row.term for row in comma.rows] == ["run", "swim"]
    assert [row.term for row in tab.rows] == ["run", "swim"]


def test_parse_handles_bom_and_header() -> None:
    parsed = parse_cards_csv("\ufeffterm;translation;notes\nrun;бежать;спорт\n")

    assert len(parsed.rows) == 1
    assert parsed.rows[0].term == "run"


def test_a_word_named_term_is_not_swallowed_as_a_header() -> None:
    parsed = parse_cards_csv("term;значение\n")

    assert len(parsed.rows) == 1
    assert parsed.rows[0].term == "term"
    assert parsed.rows[0].translation == "значение"


def test_rows_without_term_are_skipped_and_missing_translations_counted() -> None:
    parsed = parse_cards_csv("run;бежать\n;дом\nswim;\n")

    assert [row.term for row in parsed.rows] == ["run", "swim"]
    assert parsed.rows[1].translation is None
    assert parsed.skipped == 1
    assert parsed.empty_translations == 1


def test_short_rows_are_padded_and_extra_cells_ignored() -> None:
    parsed = parse_cards_csv("run;бежать\nswim;плавать;extra;cells;here;more\n")

    assert parsed.rows[0].transcription is None
    assert parsed.rows[1].term == "swim"


def test_tags_split_on_pipe_and_comma() -> None:
    parsed = parse_cards_csv("run;бежать;;;;;sport|basic,verbs;\n")

    assert parsed.rows[0].tags == ("sport", "basic", "verbs")


def test_rows_beyond_the_limit_are_skipped() -> None:
    text = "".join(f"w{i};п{i}\n" for i in range(MAX_ROWS + 5))

    parsed = parse_cards_csv(text)

    assert len(parsed.rows) == MAX_ROWS
    assert parsed.skipped == 5


def test_empty_input_yields_nothing() -> None:
    parsed = parse_cards_csv("  \n\n")

    assert parsed.rows == []
    assert parsed.skipped == 0
