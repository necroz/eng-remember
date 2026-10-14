"""A practice quiz built from the cards the learner already knows.

Deliberately outside the schedule: nothing here writes to the database, and a
question answered during a session cannot change what the next question asks —
the questions are built once from a snapshot of the collection. Practice is for
the days the queue is empty, not for moving intervals.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass

from eng_remember.config import Settings
from eng_remember.domain.answers import translation_variants
from eng_remember.models.card import Card
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardRepository

__all__ = ["MIN_PRACTICE_CARDS", "PracticeService", "QuizQuestion"]

# Four options are four distinct meanings, so a quiz needs at least that many
# distinct meanings in the collection.
MIN_PRACTICE_CARDS = 4
DISTRACTORS = 3


def _variant_families(translations: Iterable[str]) -> list[set[str]]:
    """Translations grouped by shared meaning.

    "ужасный" and "ужасный, кошмарный" are one meaning family: grading accepts
    either wording, so the quiz must not offer one as a wrong option for the
    other.
    """
    families: list[set[str]] = []
    for translation in translations:
        variants = set(translation_variants(translation))
        if not variants:
            continue
        for family in families:
            if family & variants:
                family |= variants
                break
        else:
            families.append(variants)
    return families


@dataclass(frozen=True, slots=True)
class QuizQuestion:
    """One multiple-choice question: the term and its shuffled options."""

    card_id: int
    term: str
    options: tuple[str, ...]
    correct_index: int


class PracticeService:
    def __init__(
        self,
        *,
        cards: CardRepository,
        settings: Settings,
        rng: random.Random | None = None,
    ) -> None:
        self._cards = cards
        self._settings = settings
        self._rng = rng or random.Random()

    async def available(self, user: User) -> bool:
        """Whether a quiz can be built at all, for offering one on an empty queue."""
        pool = await self._cards.list_seen(user.id)
        return len(_variant_families(card.translation for card in pool)) >= MIN_PRACTICE_CARDS

    async def build_session(self, user: User) -> list[QuizQuestion]:
        pool = await self._cards.list_seen(user.id)
        if len(_variant_families(card.translation for card in pool)) < MIN_PRACTICE_CARDS:
            return []

        size = self._settings.bot.practice_size
        chosen = self._rng.sample(pool, k=min(size, len(pool)))

        questions: list[QuizQuestion] = []
        for card in chosen:
            question = self._question_for(card, pool)
            if question is not None:
                questions.append(question)
        return questions

    def _question_for(self, card: Card, pool: list[Card]) -> QuizQuestion | None:
        """One question, or ``None`` when the card cannot fill four options."""
        correct = set(translation_variants(card.translation))
        used: list[set[str]] = [correct]
        same_pos: list[str] = []
        others: list[str] = []

        for other in pool:
            if other.id == card.id:
                continue
            variants = set(translation_variants(other.translation))
            if not variants or any(variants & family for family in used):
                continue
            used.append(variants)
            if card.part_of_speech and other.part_of_speech == card.part_of_speech:
                same_pos.append(other.translation)
            else:
                others.append(other.translation)

        self._rng.shuffle(same_pos)
        self._rng.shuffle(others)
        distractors = [*same_pos, *others][:DISTRACTORS]
        if len(distractors) < DISTRACTORS:
            return None

        options = [card.translation, *distractors]
        self._rng.shuffle(options)
        return QuizQuestion(
            card_id=card.id,
            term=card.term,
            options=tuple(options),
            correct_index=options.index(card.translation),
        )
