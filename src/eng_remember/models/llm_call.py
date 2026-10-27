from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, func, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from eng_remember.db.base import Base, UtcDateTime

if TYPE_CHECKING:
    from eng_remember.models.user import User


class LlmCall(Base):
    """Audit trail of every model call.

    Prompt text and raw answers are stored only when ``LOG_LLM_PAYLOADS`` is on,
    because they contain whatever the user typed. Token counts, latency and the
    failure reason are always recorded: without them a bad parse is
    undebuggable, and this table is also the basis for a future per-user quota.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (
        Index("ix_llm_calls_created_at", "created_at"),
        Index("ix_llm_calls_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    purpose: Mapped[str] = mapped_column(String(32), doc="Why the model was called")
    provider_model: Mapped[str] = mapped_column(String(128))
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    request_text: Mapped[str | None] = mapped_column(Text)
    response_text: Mapped[str | None] = mapped_column(Text)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    success: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())

    user: Mapped[User | None] = relationship(lazy="raise")

    def __repr__(self) -> str:
        return f"<LlmCall id={self.id} purpose={self.purpose} success={self.success}>"
