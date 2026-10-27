"""llm_calls user created index

Revision ID: 420f510e800f
Revises: 264f4235eef5
Create Date: 2026-10-27 15:07:06.889301

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "420f510e800f"
down_revision: str | None = "264f4235eef5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_llm_calls_user_id_created_at",
        "llm_calls",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_llm_calls_user_id_created_at", table_name="llm_calls")
