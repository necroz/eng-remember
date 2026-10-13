"""Choosing what to say after an answer.

The bank and the no-repeat rule live in :mod:`eng_remember.domain.praise`; this
service adds the one thing the domain cannot do — remembering, across sessions,
which templates this particular learner has already seen.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any

from eng_remember.domain.enums import ReviewRating
from eng_remember.domain.praise import (
    PraiseCategory,
    PraisePick,
    category_for_rating,
    select,
)
from eng_remember.models.user import User

__all__ = ["PraiseService"]


class PraiseService:
    def __init__(self, *, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()

    @property
    def rng(self) -> random.Random:
        return self._rng

    def pick(
        self,
        user: User,
        *,
        rating: ReviewRating,
        milestone: int | None = None,
    ) -> PraisePick:
        """Select a template and record it on the user.

        The user row is mutated rather than saved here: the caller's session owns
        the transaction, and a missing flush would be silently visible only as
        praise starting to repeat. Tests assert the history actually persists.
        """
        category = (
            PraiseCategory.MILESTONE
            if milestone is not None
            else category_for_rating(rating.name.lower())
        )
        pick = select(category, self._history(user), rng=self._rng)
        user.praise_history = pick.history
        return pick

    @staticmethod
    def _history(user: User) -> dict[str, list[int]]:
        """History as stored, defensively typed.

        The column is JSON, so anything could be in there after a manual edit;
        the selector itself already tolerates nonsense indices.
        """
        raw: Mapping[str, Any] = user.praise_history or {}
        return {
            str(key): [int(item) for item in value if isinstance(item, int)]
            for key, value in raw.items()
            if isinstance(value, list)
        }
