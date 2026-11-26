"""Rules for checking a typed answer against a card's translation.

Pure functions: the bot layer decides what to ask, the model may soften a near
miss, but what counts as a match and which grade a verdict earns are rules of
the domain — tested without a database or a bot.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from enum import StrEnum

from eng_remember.domain.enums import ReviewRating
from eng_remember.domain.languages import NATIVE_LANGUAGE, resolve_language

__all__ = ["Verdict", "matches", "normalize", "rating_for", "translation_variants"]

# Punctuation is structure, not meaning: "вездесущий!" answers the same card as
# "вездесущий". Hyphens are included so "по-русски" and "по русски" meet each
# other instead of going to the model over a dash. The non-ASCII marks matter
# for Chinese, Japanese and Arabic answers, where the sentence does not end in
# an ASCII full stop. Apostrophes are deleted rather than spaced: "dont" and
# "don't" are one answer, while a space would turn either into two words.
# The deletion does create rare collisions ("were" and "we’re", "its" and
# "it’s"); exact matching settles most of them, and the model checker sees the
# learner's typed form for the rest.
_PUNCTUATION = str.maketrans(dict.fromkeys('.,!?;:()"«»„“”[]{}…—–-|/،؛؟。、「」『』', " "))
_APOSTROPHES = str.maketrans(dict.fromkeys("'’", ""))

_VARIANT_SEPARATORS = re.compile(r"[,;/|\n]+")

# Arabic answers often carry harakat or a tatweel that a learner cannot be
# expected to reproduce. The letters themselves are folded to the forms used in
# dictionaries: alef variants to bare alef, alef maqsura and hamza-on-ya to ya.
_ARABIC_MARKS = re.compile(r"[\u064b-\u065f\u0670\u0640]")
_ARABIC_LETTERS = str.maketrans(
    {
        "آ": "ا",
        "أ": "ا",
        "إ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ئ": "ي",
    }
)


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


def normalize(text: str, language: str = NATIVE_LANGUAGE) -> str:
    """One comparison form: NFKC casefold, punctuation gone.

    Russian additionally folds ё to е; Arabic loses harakat and tatweel and
    folds its alef/ya variants. Other languages keep their diacritics, because
    in French ``café`` and ``cafe`` are not the same spelling.
    """
    code = resolve_language(language)
    normalized = unicodedata.normalize("NFKC", text).casefold()
    if code == "rus":
        normalized = normalized.replace("ё", "е")
    elif code == "arb":
        normalized = _ARABIC_MARKS.sub("", normalized).translate(_ARABIC_LETTERS)
    return " ".join(normalized.translate(_APOSTROPHES).translate(_PUNCTUATION).split())


def translation_variants(text: str) -> list[str]:
    """Every meaning a card lists, as separate normalized variants.

    The whole line is a variant too: a card for "ужасный, кошмарный" is
    answered correctly by either single meaning or by both at once.
    """
    whole = normalize(text)
    parts = [normalize(part) for part in _VARIANT_SEPARATORS.split(text)]
    return [variant for variant in dict.fromkeys([whole, *parts]) if variant]


def _meaning_words(text: str) -> list[str]:
    """Words of every meaning the card lists, as one normalized bag.

    The translation is a dictionary gloss: ``"влажный, сырой"`` is two
    acceptable meanings, and a learner may answer either one or repeat several
    of them together ("сырой влажный"). Each comma/slash-separated part
    contributes its words; the bag caps how many times a word may appear.
    """
    words: list[str] = []
    for part in _VARIANT_SEPARATORS.split(text):
        words.extend(normalize(part).split())
    return words


def matches(answer: str, translation: str, language: str = NATIVE_LANGUAGE) -> bool:
    """True when the answer repeats a meaning the card lists.

    Russian meanings may list alternatives separated by commas; a studied
    language answer is one spelling and is compared as a whole, so a comma
    inside a French term cannot silently accept half of it.
    """
    if resolve_language(language) == NATIVE_LANGUAGE:
        expected = translation_variants(translation)
        if not set(translation_variants(answer)).isdisjoint(expected):
            return True
        # Compare against the list of listed words rather than one string: the
        # same meanings in another order ("сырой влажный" for "влажный, сырой")
        # are the same answer, while an extra word is not.
        allowed = Counter(_meaning_words(translation))
        given = Counter(normalize(answer).split())
        return bool(given) and all(count <= allowed[word] for word, count in given.items())
    return normalize(answer, language) == normalize(translation, language)


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
