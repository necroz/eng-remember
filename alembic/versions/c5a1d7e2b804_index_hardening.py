"""index hardening

Makes the database agree with two rules the application already follows, and
drops indexes that only duplicate wider ones.

* Term uniqueness becomes case-insensitive. The repository finds duplicates
  with ``func.lower(term)``; the old constraint on the raw column let a race
  store both ``run`` and ``Run``. A functional unique index replaces it.
* Reviews get ``(user_id, card_id, reviewed_at)`` for the "first answer per
  card" grouping: the query reads all three columns from the index.
* Redundant single-column indexes are gone (see the comments at each drop).

If production already holds two rows that differ only in case (possible only
by bypassing the service layer), ``CREATE UNIQUE INDEX`` fails with a duplicate
key error; the transaction rolls back and no data is touched. Check beforehand
and merge the rows by hand::

    SELECT user_id, deck_id, lower(term), language, direction, count(*)
    FROM cards
    GROUP BY 1, 2, 3, 4, 5
    HAVING count(*) > 1;

All tables involved are small, so a single transactional migration is cheaper
than a concurrent build and avoids leaving an invalid index behind.

Downgrade restores the old indexes and the case-sensitive constraint. It does
not delete anything: the functional index never allowed two rows that differ
only in case, so the old constraint cannot be violated by existing data.

Revision ID: c5a1d7e2b804
Revises: b7c1d9e4f2a3
Create Date: 2026-11-15 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5a1d7e2b804"
down_revision: str | None = "b7c1d9e4f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The constraint's implicit index occupies the name until the constraint is
    # gone, so the drop has to come first.
    op.drop_constraint("uq_cards_user_id_deck_id_term_language_direction", "cards", type_="unique")
    op.create_index(
        "uq_cards_user_id_deck_id_term_language_direction",
        "cards",
        ["user_id", "deck_id", sa.text("lower(term)"), "language", "direction"],
        unique=True,
    )

    # ``count_cards_introduced_since`` and ``introduced_counts`` group answers
    # by card and take ``min(reviewed_at)``; with all three columns in the index
    # that is an index-only scan instead of a heap visit per review.
    op.create_index(
        "ix_reviews_user_id_card_id_reviewed_at",
        "reviews",
        ["user_id", "card_id", "reviewed_at"],
    )

    # ``ix_cards_user_id`` and ``ix_cards_deck_id`` are the leading columns of
    # ``ix_cards_user_id_due_at`` / ``ix_cards_deck_id_due_at``. ``ix_cards_due_at``
    # goes too, but not as a prefix: no query filters or sorts by ``due_at``
    # without a ``user_id``, and inside that scope the composite indexes end in
    # ``due_at``.
    op.drop_index("ix_cards_user_id", table_name="cards")
    op.drop_index("ix_cards_deck_id", table_name="cards")
    op.drop_index("ix_cards_due_at", table_name="cards")
    # ``ix_cards_term`` cannot serve the duplicate lookup: it compares
    # ``lower(term)``. Term ordering is always inside one learner's small set,
    # where the sort is cheaper than another index on every insert.
    op.drop_index("ix_cards_term", table_name="cards")

    # Both composite review indexes start with user_id.
    op.drop_index("ix_reviews_user_id", table_name="reviews")

    # ``uq_streaks_user_id`` and ``uq_decks_user_id_name`` already index these
    # columns as their leading key; the separate one-column indexes only add
    # write amplification.
    op.drop_index("ix_streaks_user_id", table_name="streaks")
    op.drop_index("ix_decks_user_id", table_name="decks")


def downgrade() -> None:
    op.create_index("ix_decks_user_id", "decks", ["user_id"])
    op.create_index("ix_streaks_user_id", "streaks", ["user_id"])
    op.create_index("ix_reviews_user_id", "reviews", ["user_id"])
    op.create_index("ix_cards_term", "cards", ["term"])
    op.create_index("ix_cards_due_at", "cards", ["due_at"])
    op.create_index("ix_cards_deck_id", "cards", ["deck_id"])
    op.create_index("ix_cards_user_id", "cards", ["user_id"])

    op.drop_index("ix_reviews_user_id_card_id_reviewed_at", table_name="reviews")

    op.drop_index("uq_cards_user_id_deck_id_term_language_direction", table_name="cards")
    op.create_unique_constraint(
        "uq_cards_user_id_deck_id_term_language_direction",
        "cards",
        ["user_id", "deck_id", "term", "language", "direction"],
    )
