"""Domain enumerations.

The integer values deliberately mirror ``fsrs.Rating`` and ``fsrs.State`` so a
value can travel between this layer and py-fsrs without translation.
``tests/test_domain_enums.py`` asserts that mirror stays intact across upgrades
of the library.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

__all__ = ["CardDirection", "CardState", "ReviewRating"]


class CardDirection(StrEnum):
    """Which side of a word a card asks about.

    Every word is a pair: the forward card shows the English term and asks for
    the meaning, the reverse one shows the meaning and asks for the term. Each
    has its own FSRS state — recognition and recall are different memories and
    must not share one schedule.
    """

    FORWARD = "forward"
    REVERSE = "reverse"

    @property
    def flipped(self) -> CardDirection:
        return CardDirection.REVERSE if self is CardDirection.FORWARD else CardDirection.FORWARD


class ReviewRating(IntEnum):
    """How well the learner recalled the word, mapped to FSRS grades."""

    AGAIN = 1
    HARD = 2
    GOOD = 3
    EASY = 4

    @property
    def label(self) -> str:
        return {
            ReviewRating.AGAIN: "Не помню",
            ReviewRating.HARD: "Трудно",
            ReviewRating.GOOD: "Нормально",
            ReviewRating.EASY: "Легко",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            ReviewRating.AGAIN: "🔴",
            ReviewRating.HARD: "🟠",
            ReviewRating.GOOD: "🟢",
            ReviewRating.EASY: "🔵",
        }[self]


class CardState(IntEnum):
    """Position of a card in the FSRS pipeline."""

    LEARNING = 1
    REVIEW = 2
    RELEARNING = 3

    @property
    def label(self) -> str:
        return {
            CardState.LEARNING: "изучается",
            CardState.REVIEW: "на повторении",
            CardState.RELEARNING: "переучивается",
        }[self]
