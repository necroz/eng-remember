from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from eng_remember.db.base import Base, TimestampMixin, UtcDateTime
from eng_remember.domain.enums import CardState
from eng_remember.domain.srs import CardSchedule
from eng_remember.utils.time import ensure_utc

if TYPE_CHECKING:
    from eng_remember.models.deck import Deck
    from eng_remember.models.review import Review
    from eng_remember.models.user import User

# ReviewState is persisted as a short string with a CHECK constraint rather than
# a native PostgreSQL enum: renaming a value is then a plain UPDATE instead of a
# type migration, and the same DDL runs on SQLite in tests.
_CARD_STATE_TYPE = Enum(
    CardState,
    name="card_state",
    native_enum=False,
    length=16,
    values_callable=lambda enum: [member.name.lower() for member in enum],
)


class Card(Base, TimestampMixin):
    """A vocabulary item plus the memory state FSRS needs to schedule it."""

    __tablename__ = "cards"
    __table_args__ = (
        UniqueConstraint("user_id", "deck_id", "term", name="uq_cards_user_id_deck_id_term"),
        Index("ix_cards_user_id_due_at", "user_id", "due_at"),
        Index("ix_cards_deck_id_due_at", "deck_id", "due_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    deck_id: Mapped[int] = mapped_column(ForeignKey("decks.id", ondelete="CASCADE"), index=True)

    # --- content -----------------------------------------------------------
    term: Mapped[str] = mapped_column(String(255), index=True, doc="English word or phrase")
    translation: Mapped[str] = mapped_column(Text, doc="Russian meaning")
    transcription: Mapped[str | None] = mapped_column(String(128))
    part_of_speech: Mapped[str | None] = mapped_column(String(32))
    example: Mapped[str | None] = mapped_column(Text)
    # JSONB on PostgreSQL: plain ``json`` has no equality operator, which breaks
    # alembic's server-default comparison (``'[]'::json = '[]'``).
    examples: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        default=list,
        server_default=text("'[]'"),
    )
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    cefr: Mapped[str | None] = mapped_column(String(2))
    frequency: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32), default="manual", server_default="manual")

    # --- FSRS memory state -------------------------------------------------
    due_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    state: Mapped[CardState] = mapped_column(
        _CARD_STATE_TYPE,
        default=CardState.LEARNING,
        server_default=CardState.LEARNING.name.lower(),
    )
    step: Mapped[int | None] = mapped_column(SmallInteger)
    stability: Mapped[float | None] = mapped_column(Float)
    difficulty: Mapped[float | None] = mapped_column(Float)
    last_review_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    reps: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lapses: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Every failed answer, in any state. FSRS lapses only grow once a card
    # leaves learning, which would hide the words stuck in the earliest stage.
    again_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    leech_level: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")

    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    user: Mapped[User] = relationship(back_populates="cards", lazy="raise")
    deck: Mapped[Deck] = relationship(back_populates="cards", lazy="raise")
    reviews: Mapped[list[Review]] = relationship(
        back_populates="card",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    @property
    def schedule(self) -> CardSchedule:
        """Scheduling columns as a domain object.

        Timestamps are normalised to UTC here. PostgreSQL returns them with an
        offset, SQLite does not — and a naive datetime reaching the scheduler
        would raise only at the moment a real user answered a real card.
        """
        return CardSchedule(
            due_at=ensure_utc(self.due_at) or self.due_at,
            state=self.state,
            step=self.step,
            stability=self.stability,
            difficulty=self.difficulty,
            last_review_at=ensure_utc(self.last_review_at),
            reps=self.reps,
            lapses=self.lapses,
        )

    def apply_schedule(self, schedule: CardSchedule) -> None:
        """Write a domain schedule back onto the row."""
        self.due_at = schedule.due_at
        self.state = schedule.state
        self.step = schedule.step
        self.stability = schedule.stability
        self.difficulty = schedule.difficulty
        self.last_review_at = schedule.last_review_at
        self.reps = schedule.reps
        self.lapses = schedule.lapses

    def __repr__(self) -> str:
        return f"<Card id={self.id} term={self.term!r} due={self.due_at:%Y-%m-%d %H:%M}>"
