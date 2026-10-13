"""Domain enumerations.

The integer values deliberately mirror ``fsrs.Rating`` and ``fsrs.State`` so a
value can travel between this layer and py-fsrs without translation.
``tests/test_domain_enums.py`` asserts that mirror stays intact across upgrades
of the library.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = ["CardState", "ReviewRating"]


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
