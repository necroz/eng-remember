from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from eng_remember.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from eng_remember.models.card import Card
    from eng_remember.models.user import User


class Deck(Base, TimestampMixin):
    """A named bucket of cards. Every user gets one default deck on /start."""

    __tablename__ = "decks"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_decks_user_id_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(String(512))
    # The domain or context this deck currently collects words from. Set when
    # the learner adds words from a topic or from a text, and read back when they
    # ask for "more words like these" without repeating the subject.
    topic: Mapped[str | None] = mapped_column(String(160))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    user: Mapped[User] = relationship(back_populates="decks", lazy="raise")
    cards: Mapped[list[Card]] = relationship(
        back_populates="deck",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<Deck id={self.id} name={self.name!r}>"
