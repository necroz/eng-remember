from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from eng_remember.db.base import Base, TimestampMixin
from eng_remember.domain.gamification import StreakState

if TYPE_CHECKING:
    from eng_remember.models.user import User


class Streak(Base, TimestampMixin):
    """One row per learner: the current streak and its freezes.

    A separate table rather than columns on ``users``: the streak is a subsystem
    with its own rules, and the table is the natural place for it to grow
    (freeze history, weekly goals) without widening the row every request reads.
    """

    __tablename__ = "streaks"
    __table_args__ = (UniqueConstraint("user_id", name="uq_streaks_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    current: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    longest: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    freezes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    missed_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    accounted_on: Mapped[date | None] = mapped_column(Date)

    user: Mapped[User] = relationship(back_populates="streak", lazy="raise")

    @property
    def state(self) -> StreakState:
        """The row as a domain object."""
        return StreakState(
            current=self.current,
            longest=self.longest,
            freezes=self.freezes,
            missed_days=self.missed_days,
            accounted_on=self.accounted_on,
        )

    def apply(self, state: StreakState) -> None:
        """Write a domain state back onto the row."""
        self.current = state.current
        self.longest = state.longest
        self.freezes = state.freezes
        self.missed_days = state.missed_days
        self.accounted_on = state.accounted_on

    def __repr__(self) -> str:
        return f"<Streak user_id={self.user_id} current={self.current} freezes={self.freezes}>"
