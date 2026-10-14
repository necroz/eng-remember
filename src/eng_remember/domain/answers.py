"""Rules for checking a typed answer against a card's translation.

Pure functions: the bot layer decides what to ask, the model may soften a near
miss, but what counts as a match and which grade a verdict earns are rules of
the domain — tested without a database or a bot.
"""

from __future__ import annotations

import re
from enum import StrEnum

from eng_remember.domain.enums import ReviewRating

__all__ = ["Verdict", "matches", "normalize", "rating_for", "translation_variants"]

# Punctuation is structure, not meaning: "вездесущий!" answers the same card as
# "вездесущий". Hyphens are included so "по-русски" and "по русски" meet each
# other instead of going to the model over a dash.
_PUNCTUATION = str.maketrans(dict.fromkeys('.,!?;:()"«»„“”[]{}…—–-|/', " "))

_VARIANT_SEPARATORS = re.compile(r"[,;/|\n]+")


class Verdict(StrEnum):
    """How close a typed answer came to the card's meaning."""

    CORRECT = "correct"
    ALMOST = "almost"
    WRONG = "wrong"

    @property
    def label(self) -> str:
        return {
            Verdict.CORRECT: "Верно",
            Verdict.ALMOST: "Почти",
            Verdict.WRONG: "Неверно",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            Verdict.CORRECT: "✅",
            Verdict.ALMOST: "🟡",
            Verdict.WRONG: "❌",
        }[self]


def normalize(text: str) -> str:
    """One comparison form: lowercase, ё folded to е, punctuation gone."""
    lowered = text.lower().replace("ё", "е")
    return " ".join(lowered.translate(_PUNCTUATION).split())


def translation_variants(text: str) -> list[str]:
    """Every meaning a card lists, as separate normalized variants.

    The whole line is a variant too: a card for "ужасный, кошмарный" is
    answered correctly by either single meaning or by both at once.
    """
    whole = normalize(text)
    parts = [normalize(part) for part in _VARIANT_SEPARATORS.split(text)]
    return [variant for variant in dict.fromkeys([whole, *parts]) if variant]


def matches(answer: str, translation: str) -> bool:
    """True when the answer repeats any meaning the card lists."""
    return not set(translation_variants(answer)).isdisjoint(translation_variants(translation))


def rating_for(
    verdict: Verdict,
    *,
    duration_seconds: float | None,
    easy_threshold_seconds: float,
    hint_used: bool = False,
) -> ReviewRating:
    """The FSRS grade a verdict earns.

    Speed only separates the two correct grades: a fast correct answer is
    "easy", a slow one is only "good". A missing duration counts as slow — a
    card answered after a break must not collect the easy bonus for it.

    A hint, when one was shown, caps the grade at "good": the answer is real,
    but it was reached with the context on screen, and that is not the recall
    "easy" is meant to reward.
    """
    if verdict is Verdict.CORRECT:
        if hint_used:
            return ReviewRating.GOOD
        if duration_seconds is not None and duration_seconds < easy_threshold_seconds:
            return ReviewRating.EASY
        return ReviewRating.GOOD
    if verdict is Verdict.ALMOST:
        return ReviewRating.HARD
    return ReviewRating.AGAIN
