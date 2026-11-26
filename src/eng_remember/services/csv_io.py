"""Reading and writing the CSV exchange format.

Pure functions over text: the export side turns cards into a file, the import
side turns a file into rows. No database and no model live here — enrichment,
de-duplication and saving belong to the flow that calls these functions.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import chain

from eng_remember.domain.languages import DEFAULT_LANGUAGE
from eng_remember.models.card import Card

__all__ = [
    "CSV_DELIMITER",
    "MAX_FILE_BYTES",
    "MAX_ROWS",
    "CsvParseResult",
    "CsvRow",
    "cards_to_csv",
    "cards_to_csv_parts",
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

# Delimiters the sniffer considers, in tie-break order.
_SNIFF_DELIMITERS = (";", ",", "\t")
# How many records vote on the delimiter. A single record is easy to mislead
# with a punctuation-heavy translation, a handful is not. Records (not physical
# lines) are counted so a quoted field holding newlines stays whole.
_SNIFF_SAMPLE_RECORDS = 10


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
    """Serialize cards as one CSV document."""
    return "".join(_csv_lines(cards, deck_names or {}))


def cards_to_csv_parts(
    cards: Sequence[Card], *, deck_names: Mapping[int, str] | None = None
) -> list[bytes]:
    """Encode importable files, each with its own BOM and header."""
    lines = _csv_lines(cards, deck_names or {})
    header = next(lines).encode("utf-8-sig")
    parts: list[bytes] = []
    part = bytearray(header)
    rows = 0
    for line in lines:
        encoded = line.encode("utf-8")
        if len(header) + len(encoded) > MAX_FILE_BYTES:
            raise ValueError("a single card exceeds the CSV file limit")
        if rows >= MAX_ROWS or len(part) + len(encoded) > MAX_FILE_BYTES:
            parts.append(bytes(part))
            part = bytearray(header)
            rows = 0
        part.extend(encoded)
        rows += 1
    parts.append(bytes(part))
    return parts


def _csv_lines(cards: Sequence[Card], names: Mapping[int, str]) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=CSV_DELIMITER, lineterminator="\r\n")
    rows = (
        (
            card.term,
            card.translation,
            card.transcription or "",
            card.part_of_speech or "",
            card.example or "",
            card.notes or "",
            "|".join(card.tags),
            names.get(card.deck_id, ""),
            card.language or DEFAULT_LANGUAGE,
        )
        for card in cards
    )
    for row in chain((_HEADER,), rows):
        writer.writerow(row)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()


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
    """The delimiter that gives a stable column count across the sample records.

    ``csv.Sniffer`` gets confused by commas inside a semicolon-delimited row
    (tags like "sport,basic"), and one record can be misleading when a
    translation holds other punctuation. So up to ten records vote: the
    delimiter whose sample has the largest share of records sharing the same
    column count above one wins. Voting over records rather than raw lines
    keeps a quoted field holding newlines together; the share, not the raw
    count, is compared because a wrong delimiter can split one logical row into
    several records and so collect more votes for itself. Ties keep the order
    ``;``, ``,``, tab.
    """
    best = CSV_DELIMITER
    best_votes = 0
    best_sampled = 0
    for delimiter in _SNIFF_DELIMITERS:
        votes, sampled = _split_agreement(text, delimiter)
        if votes == 0:
            continue
        if best_votes == 0 or votes * best_sampled > best_votes * sampled:
            best = delimiter
            best_votes = votes
            best_sampled = sampled
    return best


def _split_agreement(text: str, delimiter: str) -> tuple[int, int]:
    """Votes for a delimiter as (agreeing records, sampled records).

    The votes are the number of sample records sharing the most common column
    count above one. Empty records do not vote.
    """
    counts: list[int] = []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        while len(counts) < _SNIFF_SAMPLE_RECORDS:
            try:
                record = next(reader)
            except StopIteration:
                break
            if not record or not any(cell.strip() for cell in record):
                continue
            counts.append(len(record))
    except csv.Error:
        pass
    if not counts:
        return 0, 0
    mode = Counter(counts).most_common(1)[0][0]
    if mode <= 1:
        return 0, len(counts)
    return counts.count(mode), len(counts)


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
