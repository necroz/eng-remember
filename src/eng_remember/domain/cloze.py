"""Turning an example sentence into a cloze hint.

Pure text work: the review flow asks the learner to recall a translation, and
the example with the term blanked out is the context cue that makes the recall
grounded in real usage instead of a bare word.

Finding the term is language-aware and lives in :mod:`eng_remember.domain.term_match`.
The failure direction stays conservative: whenever the term cannot be recognised
with confidence, the example is returned untouched rather than partially
blanked, because a sentence with the wrong words hidden is worse than no hint at
all.
"""

from __future__ import annotations

from eng_remember.domain.languages import DEFAULT_LANGUAGE
from eng_remember.domain.term_match import term_spans

__all__ = ["BLANK", "cloze"]

BLANK = "____"


def cloze(example: str, term: str, language: str = DEFAULT_LANGUAGE) -> str:
    """The example with every occurrence of the term replaced by a blank."""
    spans = term_spans(example, term, language)
    if not spans:
        return example

    result = example
    for start, end in reversed(spans):
        result = f"{result[:start]}{BLANK}{result[end:]}"
    return result
