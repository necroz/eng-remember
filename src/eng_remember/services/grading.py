"""Grading a typed answer: simple match, model escalation, FSRS grade.

Sits between the review router and the review service: the router knows what
the learner typed and how long they took, the review service knows the
schedule — deciding what the answer earns belongs here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog

from eng_remember.domain.answers import Verdict, matches, rating_for
from eng_remember.domain.enums import CardDirection, ReviewRating
from eng_remember.domain.languages import NATIVE_LANGUAGE
from eng_remember.errors import LlmError
from eng_remember.models.card import Card
from eng_remember.models.user import User
from eng_remember.services.llm.checker import AnswerChecker
from eng_remember.services.review import AnswerResult, ReviewService

__all__ = ["AnswerGradingService", "GradeOutcome"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GradeOutcome:
    """The verdict, the grade it earned and the recorded answer."""

    verdict: Verdict
    rating: ReviewRating
    typed: str | None
    answer: AnswerResult


class AnswerGradingService:
    def __init__(
        self,
        *,
        reviews: ReviewService,
        checker: AnswerChecker,
        easy_threshold_seconds: float,
    ) -> None:
        self._reviews = reviews
        self._checker = checker
        self._easy_threshold_seconds = easy_threshold_seconds

    async def grade(
        self,
        user: User,
        card: Card,
        *,
        typed: str | None,
        duration_seconds: float | None,
        hint_used: bool = False,
        now: datetime | None = None,
    ) -> GradeOutcome:
        """Grade one answer and write it into the card's schedule."""
        verdict = await self._verdict(card, typed, user_id=user.id)
        rating = rating_for(
            verdict,
            duration_seconds=duration_seconds,
            easy_threshold_seconds=self._easy_threshold_seconds,
            hint_used=hint_used,
        )
        duration_ms = int(duration_seconds * 1000) if duration_seconds is not None else None
        answer = await self._reviews.answer(user, card, rating, duration_ms=duration_ms, now=now)
        return GradeOutcome(verdict=verdict, rating=rating, typed=typed, answer=answer)

    async def _verdict(self, card: Card, typed: str | None, *, user_id: int) -> Verdict:
        """Simple match first, then the model, then a conservative "wrong"."""
        if typed is None:
            return Verdict.WRONG
        reverse = card.direction is CardDirection.REVERSE
        expected = card.term if reverse else card.translation
        language = card.language if reverse else NATIVE_LANGUAGE
        if matches(typed, expected, language):
            return Verdict.CORRECT
        if not self._checker.available:
            return Verdict.WRONG
        try:
            return await self._checker.check(
                term=card.term,
                translation=card.translation,
                answer=typed,
                direction=card.direction.value,
                user_id=user_id,
            )
        except LlmError as exc:
            # A broken model cannot leave the answer ungraded, and guessing
            # "correct" for an unmatched answer would be free marks: the
            # conservative verdict is the same one the no-model path gives.
            log.warning("answer_check_failed", user_id=user_id, error=str(exc))
            return Verdict.WRONG
