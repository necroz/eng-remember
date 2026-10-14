from __future__ import annotations

from datetime import date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import JSON, BigInteger, Boolean, Date, Integer, String, Time, false, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from eng_remember.db.base import Base, TimestampMixin, UtcDateTime

if TYPE_CHECKING:
    from eng_remember.models.card import Card
    from eng_remember.models.deck import Deck
    from eng_remember.models.streak import Streak


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(8))

    timezone: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    new_cards_per_day: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    level: Mapped[str | None] = mapped_column(String(2), doc="CEFR level the learner reports")
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # Indices of already-used praise templates per category. Presentation state,
    # but it belongs to the user and has to outlive the process, so it lives here
    # rather than in the in-memory FSM.
    #
    # Deliberately no server default: Postgres has no equality operator for
    # ``json``, so a default here makes ``alembic check`` fail when it compares
    # server defaults. The migration backfills existing rows instead.
    praise_history: Mapped[dict[str, list[int]]] = mapped_column(JSON, default=dict)

    # --- reminders ---------------------------------------------------------
    # Kept on the user row rather than in a table of their own: these are three
    # settings and two daily markers, read by one periodic query, and a join on
    # every tick would buy nothing. The streak got its own table because it has
    # rules and will grow; this will not.
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # ``None`` means "follow the configured default", which is not the same as a
    # chosen time: a learner who never touched the setting should track a changed
    # default instead of being pinned to whatever it was on the day they started.
    reminder_time: Mapped[time | None] = mapped_column(Time)
    evening_reminder: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    evening_reminder_time: Mapped[time | None] = mapped_column(Time)
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    last_reminder_on: Mapped[date | None] = mapped_column(Date)
    last_evening_reminder_on: Mapped[date | None] = mapped_column(Date)
    last_digest_on: Mapped[date | None] = mapped_column(Date)

    decks: Mapped[list[Deck]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    cards: Mapped[list[Card]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="raise",
    )
    streak: Mapped[Streak | None] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} telegram_id={self.telegram_id}>"
