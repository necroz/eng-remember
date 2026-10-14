"""Turning an example sentence into a cloze hint.

Pure text work: the review flow asks the learner to recall a translation, and
the example with the term blanked out is the context cue that makes the recall
grounded in real usage instead of a bare word.

Matching is deliberately stem-based — the same four-character rule the
discovery service uses — because an example rarely contains the dictionary form
("He runs" for "run"). The failure direction is conservative: whenever the term
cannot be recognised with confidence, the example is returned untouched rather
than partially blanked, because a sentence with the wrong words hidden is worse
than no hint at all.
"""

from __future__ import annotations

import re

__all__ = ["BLANK", "cloze"]

BLANK = "____"

_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")

# How many leading characters have to match for two words to count as forms of
# each other. Same rule as the discovery service's text check.
_STEM = 4

# Below this length a stem comparison is too loose ("a" would match anything
# starting with "a"), so short words only match exactly.
_MIN_STEM_LENGTH = 2


def cloze(example: str, term: str) -> str:
    """The example with every occurrence of the term replaced by a blank."""
    parts = [part for part in term.split() if part]
    if not example or not parts:
        return example

    spans = [(match.start(), match.end(), match.group()) for match in _WORD.finditer(example)]
    replacements = _find_replacements(spans, parts)
    if not replacements:
        return example

    result = example
    for start, end in reversed(replacements):
        result = f"{result[:start]}{BLANK}{result[end:]}"
    return result


def _find_replacements(
    spans: list[tuple[int, int, str]],
    parts: list[str],
) -> list[tuple[int, int]]:
    """Spans to hide: the whole phrase as one blank, or each matching word."""
    if len(parts) == 1:
        return [(start, end) for start, end, token in spans if _matches(token, parts[0])]

    runs: list[tuple[int, int]] = []
    for offset in range(len(spans) - len(parts) + 1):
        if all(_matches(spans[offset + index][2], part) for index, part in enumerate(parts)):
            runs.append((spans[offset][0], spans[offset + len(parts) - 1][1]))
    if runs:
        return runs

    # The phrase may have been split, but a partial blank ("came ____ ____")
    # helps nobody: every part has to appear somewhere before anything is hidden.
    if not all(any(_matches(token, part) for _, _, token in spans) for part in parts):
        return []
    return [
        (start, end) for start, end, token in spans if any(_matches(token, part) for part in parts)
    ]


def _matches(token: str, word: str) -> bool:
    token_low = token.lower()
    word_low = word.lower()
    if token_low == word_low:
        return True
    if len(word_low) >= _MIN_STEM_LENGTH and token_low.startswith(word_low[:_STEM]):
        return True
    return len(token_low) >= _MIN_STEM_LENGTH and word_low.startswith(token_low[:_STEM])
