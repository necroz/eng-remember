"""The review loop: build a queue, record an answer, reschedule the card."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from eng_remember.domain.enums import CardState, ReviewRating
from eng_remember.domain.gamification import StreakTransition
from eng_remember.domain.srs import SrsScheduler
from eng_remember.models.card import Card
from eng_remember.models.review import Review
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardRepository
from eng_remember.repositories.reviews import ReviewRepository
from eng_remember.services.streak import StreakService
from eng_remember.utils.time import ensure_utc, humanize_interval, local_day_start, utcnow

__all__ = ["AnswerResult", "ReviewQueue", "ReviewService"]


def _one_per_term(cards: list[Card]) -> list[Card]:
    """At most one new card per word per queue.

    Every word is a pair; showing both directions back to back in one session
    would make the second answer a trivial echo. ``list_new`` orders by id, and
    the forward card is always created first, so the pair is introduced from
    the English side and the reverse twin waits for the next day.
    """
    seen: set[str] = set()
    kept: list[Card] = []
    for card in cards:
        key = card.term.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append(card)
    return kept


@dataclass(slots=True)
class ReviewQueue:
    """Cards to show now, plus the numbers behind that choice."""

    cards: list[Card]
    due_total: int
    new_total: int
    new_allowance: int

    def __bool__(self) -> bool:
        return bool(self.cards)

    def __len__(self) -> int:
        return len(self.cards)


@dataclass(frozen=True, slots=True)
class AnswerResult:
    card: Card
    rating: ReviewRating
    state_before: CardState
    due_at: datetime
    interval_seconds: float
    streak: StreakTransition | None = None

    @property
    def interval_text(self) -> str:
        return humanize_interval(self.interval_seconds)


class ReviewService:
    def __init__(
        self,
        *,
        cards: CardRepository,
        reviews: ReviewRepository,
        scheduler: SrsScheduler,
        streak: StreakService,
        reviews_per_session: int = 50,
        new_cards_per_day: int = 20,
    ) -> None:
        self._cards = cards
        self._reviews = reviews
        self._scheduler = scheduler
        self._streak = streak
        self._reviews_per_session = reviews_per_session
        self._new_cards_per_day = new_cards_per_day

    async def build_queue(
        self,
        user: User,
        *,
        deck_id: int | None = None,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> ReviewQueue:
        """Reviews that are due, then as many new cards as the daily budget allows.

        Cards already in rotation always come first — they are the ones the
        schedule says are about to be forgotten. New material is capped by the
        per-day budget, measured against the user's own midnight.
        """
        moment = ensure_utc(now) or utcnow()
        session_limit = limit or min(user.new_cards_per_day * 2, self._reviews_per_session)
        per_day = user.new_cards_per_day or self._new_cards_per_day

        due_cards = await self._cards.list_due(
            user.id, now=moment, limit=session_limit, deck_id=deck_id
        )

        remaining = session_limit - len(due_cards)
        introduced_today = await self._reviews.count_cards_introduced_since(
            user.id, since=local_day_start(user.timezone, now=moment)
        )
        allowance = max(0, per_day - introduced_today)

        new_cards: list[Card] = []
        if remaining > 0 and allowance > 0:
            wanted = min(remaining, allowance)
            # Candidates are fetched with a margin because the bury filters only
            # ever remove entries: both directions of a word are in the list, and
            # a twin whose sibling was answered today is skipped. Without the
            # margin a one-card queue build (get_next) would fetch exactly the
            # buried twin and report "nothing left" while words are waiting.
            candidates = await self._cards.list_new(
                user.id,
                now=moment,
                limit=min(self._reviews_per_session, max(2, wanted * 2)),
                deck_id=deck_id,
            )
            answered_today = await self._cards.terms_reviewed_since(
                user.id,
                [card.term for card in candidates],
                since=local_day_start(user.timezone, now=moment),
            )
            new_cards = [
                card
                for card in _one_per_term(candidates)
                if card.term.casefold() not in answered_today
            ][:wanted]

        return ReviewQueue(
            cards=[*due_cards, *new_cards],
            due_total=await self._cards.count_due(user.id, now=moment, deck_id=deck_id),
            new_total=await self._cards.count_new(user.id, now=moment, deck_id=deck_id),
            new_allowance=allowance,
        )

    async def get_next(
        self,
        user: User,
        *,
        deck_id: int | None = None,
        now: datetime | None = None,
    ) -> Card | None:
        queue = await self.build_queue(user, deck_id=deck_id, limit=1, now=now)
        return queue.cards[0] if queue.cards else None

    async def answer(
        self,
        user: User,
        card: Card,
        rating: ReviewRating,
        *,
        duration_ms: int | None = None,
        now: datetime | None = None,
    ) -> AnswerResult:
        """Apply an answer, persist the new schedule and log the event."""
        moment = ensure_utc(now) or utcnow()
        previous = card.schedule
        updated = self._scheduler.review(previous, rating, now=moment)

        if rating is ReviewRating.AGAIN:
            # Every failed answer counts, in any state: FSRS lapses only grow in
            # review, and the words stuck in learning are exactly the ones the
            # leech flow exists for.
            card.again_count = (card.again_count or 0) + 1

        last_review = previous.last_review_at
        scheduled_seconds = (previous.due_at - last_review).total_seconds() if last_review else 0.0
        elapsed_seconds = (moment - last_review).total_seconds() if last_review else 0.0

        await self._cards.update_schedule(card, updated)
        await self._reviews.add(
            Review(
                card_id=card.id,
                user_id=user.id,
                rating=rating,
                reviewed_at=moment,
                duration_ms=duration_ms,
                state_before=previous.state,
                state_after=updated.state,
                stability_after=updated.stability,
                difficulty_after=updated.difficulty,
                elapsed_days=elapsed_seconds / 86400,
                scheduled_days=scheduled_seconds / 86400,
            )
        )

        # The streak is advanced here rather than in the handler: "answering a
        # card counts the day" is a rule of the domain, and a rule that lives in
        # the UI is one a future handler will forget.
        streak = await self._streak.record(user, now=moment)

        return AnswerResult(
            card=card,
            rating=rating,
            state_before=previous.state,
            due_at=updated.due_at,
            interval_seconds=(updated.due_at - moment).total_seconds(),
            streak=streak,
        )
