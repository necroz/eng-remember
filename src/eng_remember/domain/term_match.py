"""Finding a vocabulary term inside a sentence.

Latin and Cyrillic scripts keep the four-character stem rule used by discovery:
an example may contain "negotiating" for "negotiate". Scripts without word
spacing or with rich morphology use normalized containment instead, and CJK
terms are exact substrings because Chinese and Japanese do not put spaces
between words.
"""

from __future__ import annotations

import re
import unicodedata

from eng_remember.domain.answers import normalize
from eng_remember.domain.languages import DEFAULT_LANGUAGE, resolve_language

__all__ = ["contains_term", "term_spans"]

_CJK_LANGUAGES = frozenset({"cmn", "jpn"})
_STEM_LANGUAGES = frozenset(
    {"eng", "fra", "spa", "por", "ind", "deu", "vie", "swh", "tur", "ita", "rus"}
)

# Same rule as discovery and the old cloze implementation: how many leading
# characters have to agree before two words count as forms of each other.
_STEM = 4
_MIN_STEM_LENGTH = 2


def term_spans(text: str, term: str, language: str = DEFAULT_LANGUAGE) -> list[tuple[int, int]]:
    """Character spans of ``term`` in ``text``, in reading order."""
    code = resolve_language(language)
    cleaned_term = term.strip()
    if not text or not cleaned_term:
        return []

    if code in _CJK_LANGUAGES:
        return [
            (match.start(), match.end())
            for match in re.finditer(re.escape(cleaned_term), text, re.IGNORECASE)
        ]

    parts = [part for part in cleaned_term.split() if part]
    if not parts:
        return []
    tokens = _tokens(text)
    if not tokens:
        return []

    part_matches: list[list[int]] = []
    for part in parts:
        matches = [
            index for index, (_, _, token) in enumerate(tokens) if _token_matches(token, part, code)
        ]
        if not matches:
            return []
        part_matches.append(matches)

    runs: list[tuple[int, int]] = []
    for start in part_matches[0]:
        if all(start + offset in part_matches[offset] for offset in range(1, len(parts))):
            runs.append((tokens[start][0], tokens[start + len(parts) - 1][1]))
    if runs:
        return runs

    # Every part is present, but not as one phrase. Hide each occurrence; a
    # partial phrase blank would be misleading, so this branch only runs when
    # all parts were found.
    return sorted(
        {(tokens[index][0], tokens[index][1]) for indices in part_matches for index in indices}
    )


def contains_term(text: str, term: str, language: str = DEFAULT_LANGUAGE) -> bool:
    """Whether ``term`` occurs in ``text`` under the language's matching rules."""
    return bool(term_spans(text, term, language))


def _tokens(text: str) -> list[tuple[int, int, str]]:
    """Unicode tokens of letters plus combining marks; punctuation separates."""
    tokens: list[tuple[int, int, str]] = []
    start: int | None = None
    for index, char in enumerate(text):
        if char.isalpha() or (start is not None and _is_mark(char)):
            if start is None:
                start = index
        elif start is not None:
            tokens.append((start, index, text[start:index]))
            start = None
    if start is not None:
        tokens.append((start, len(text), text[start:]))
    return tokens


def _is_mark(char: str) -> bool:
    return unicodedata.category(char).startswith("M")


def _token_matches(token: str, part: str, language: str) -> bool:
    folded_token = normalize(token, language)
    folded_part = normalize(part, language)
    if not folded_token or not folded_part:
        return False
    if folded_token == folded_part:
        return True
    if language in _STEM_LANGUAGES:
        if len(folded_part) >= _MIN_STEM_LENGTH and folded_token.startswith(folded_part[:_STEM]):
            return True
        return len(folded_token) >= _MIN_STEM_LENGTH and folded_part.startswith(
            folded_token[:_STEM]
        )
    return folded_part in folded_token
