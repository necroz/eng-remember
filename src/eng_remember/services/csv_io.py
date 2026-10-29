"""Reading and writing the CSV exchange format.

Pure functions over text: the export side turns cards into a file, the import
side turns a file into rows. No database and no model live here — enrichment,
de-duplication and saving belong to the flow that calls these functions.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from eng_remember.domain.languages import DEFAULT_LANGUAGE
from eng_remember.models.card import Card

__all__ = [
    "CSV_DELIMITER",
    "MAX_FILE_BYTES",
    "MAX_ROWS",
    "CsvParseResult",
    "CsvRow",
    "cards_to_csv",
    "parse_cards_csv",
]

CSV_DELIMITER = ";"
MAX_FILE_BYTES = 1_000_000
MAX_ROWS = 1000

_HEADER = (
    "term",
    "translation",
    "transcription",
    "part_of_speech",
    "example",
    "notes",
    "tags",
    "deck",
    "language",
)

# First cells that mean "this first row is a header", paired with the second
# cell: a genuine card whose term happens to be "term" has a translation there,
# not the word "translation". Both cells must match — a bare "term;" row is a
# real word waiting for its meaning, not a header.
_HEADER_TERMS = {"term", "слово"}
_HEADER_TRANSLATIONS = {"translation", "перевод"}


@dataclass(frozen=True, slots=True)
class CsvRow:
    term: str
    translation: str | None
    transcription: str | None = None
    part_of_speech: str | None = None
    example: str | None = None
    notes: str | None = None
    tags: tuple[str, ...] = ()
    deck: str | None = None
    language: str | None = None


@dataclass(slots=True)
class CsvParseResult:
    rows: list[CsvRow] = field(default_factory=list)
    skipped: int = 0
    empty_translations: int = 0


def cards_to_csv(cards: Sequence[Card], *, deck_names: Mapping[int, str] | None = None) -> str:
    """The whole collection as one file. Deck names travel with the cards so a
    round-trip can rebuild the same structure."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=CSV_DELIMITER, lineterminator="\r\n")
    writer.writerow(_HEADER)
    names = deck_names or {}

    for card in cards:
        writer.writerow(
            [
                card.term,
                card.translation,
                card.transcription or "",
                card.part_of_speech or "",
                card.example or "",
                card.notes or "",
                "|".join(card.tags),
                names.get(card.deck_id, ""),
                card.language or DEFAULT_LANGUAGE,
            ]
        )
    return buffer.getvalue()


def parse_cards_csv(text: str) -> CsvParseResult:
    """Rows the file holds, with structural skips counted rather than raised."""
    cleaned = text.lstrip("\ufeff")
    if not cleaned.strip():
        return CsvParseResult()

    reader = csv.reader(io.StringIO(cleaned), delimiter=_sniff(cleaned))
    result = CsvParseResult()
    first = True

    for raw in reader:
        if not raw or not any(cell.strip() for cell in raw):
            continue
        if first and _is_header(raw):
            first = False
            continue
        first = False

        if len(result.rows) >= MAX_ROWS:
            result.skipped += 1
            continue
        row = _to_row(raw)
        if not row.term:
            result.skipped += 1
            continue
        if row.translation is None:
            result.empty_translations += 1
        result.rows.append(row)

    return result


def _sniff(text: str) -> str:
    """The delimiter that appears most in the first non-empty line.

    ``csv.Sniffer`` gets confused by commas inside a semicolon-delimited row
    (tags like "sport,basic"), so the choice is deliberate and predictable.
    """
    first = next((line for line in text.splitlines() if line.strip()), "")
    counts = {delimiter: first.count(delimiter) for delimiter in (";", ",", "\t")}
    best = max(counts, key=lambda delimiter: counts[delimiter])
    return best if counts[best] > 0 else CSV_DELIMITER


def _is_header(raw: list[str]) -> bool:
    if not raw:
        return False
    cells = [cell.strip().casefold() for cell in raw]
    second = cells[1] if len(cells) > 1 else ""
    return cells[0] in _HEADER_TERMS and second in _HEADER_TRANSLATIONS


def _to_row(raw: list[str]) -> CsvRow:
    cells = [cell.strip() for cell in raw]
    while len(cells) < len(_HEADER):
        cells.append("")

    tags = tuple(
        tag.strip() for group in cells[6].split("|") for tag in group.split(",") if tag.strip()
    )
    return CsvRow(
        term=cells[0],
        translation=cells[1] or None,
        transcription=cells[2] or None,
        part_of_speech=cells[3] or None,
        example=cells[4] or None,
        notes=cells[5] or None,
        tags=tags,
        deck=cells[7] or None,
        language=cells[8] or None,
    )
