from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Enum, Float, ForeignKey, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from eng_remember.db.base import Base, UtcDateTime
from eng_remember.domain.enums import CardState, ReviewRating

if TYPE_CHECKING:
    from eng_remember.models.card import Card

_RATING_TYPE = Enum(
    ReviewRating,
    name="review_rating",
    native_enum=False,
    length=8,
    values_callable=lambda enum: [member.name.lower() for member in enum],
)
_STATE_TYPE = Enum(
    CardState,
    name="review_card_state",
    native_enum=False,
    length=16,
    values_callable=lambda enum: [member.name.lower() for member in enum],
)


class Review(Base):
    """Append-only log of every answer.

    The ``cards`` row only carries the *current* memory state; this table is what
    makes it possible to rebuild history, run statistics, or re-fit FSRS
    parameters later without asking users to relearn anything.
    """

    __tablename__ = "reviews"
    __table_args__ = (Index("ix_reviews_user_id_reviewed_at", "user_id", "reviewed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    rating: Mapped[ReviewRating] = mapped_column(_RATING_TYPE)
    reviewed_at: Mapped[datetime] = mapped_column(UtcDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    state_before: Mapped[CardState] = mapped_column(_STATE_TYPE)
    state_after: Mapped[CardState] = mapped_column(_STATE_TYPE)
    stability_after: Mapped[float | None] = mapped_column(Float)
    difficulty_after: Mapped[float | None] = mapped_column(Float)
    elapsed_days: Mapped[float | None] = mapped_column(Float, doc="Days since the previous review")
    scheduled_days: Mapped[float | None] = mapped_column(Float, doc="Interval that was planned")

    card: Mapped[Card] = relationship(back_populates="reviews", lazy="raise")

    def __repr__(self) -> str:
        return f"<Review id={self.id} card_id={self.card_id} rating={self.rating.name}>"
