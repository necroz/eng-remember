"""card language

Revision ID: b7c1d9e4f2a3
Revises: 420f510e800f
Create Date: 2026-10-29 16:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c1d9e4f2a3"
down_revision: str | None = "420f510e800f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cards",
        sa.Column("language", sa.String(length=8), server_default="eng", nullable=False),
    )
    op.drop_constraint("uq_cards_user_id_deck_id_term_direction", "cards", type_="unique")
    op.create_unique_constraint(
        "uq_cards_user_id_deck_id_term_language_direction",
        "cards",
        ["user_id", "deck_id", "term", "language", "direction"],
    )


def downgrade() -> None:
    # The old key cannot represent the same word in two languages. Keeping only
    # English is the least surprising rollback for a feature that did not exist
    # before this migration, and it mirrors the direction-twin rollback.
    op.execute("DELETE FROM cards WHERE language <> 'eng'")
    op.drop_constraint(
        "uq_cards_user_id_deck_id_term_language_direction",
        "cards",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_cards_user_id_deck_id_term_direction",
        "cards",
        ["user_id", "deck_id", "term", "direction"],
    )
    op.drop_column("cards", "language")
