"""Индексы, от которых зависит корректность и скорость горячих запросов.

SQLite здесь не только тестовая СУБД: бот создаёт схему через ``create_all``,
поэтому функциональный уникальный индекс должен работать и без миграции.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from tests.factories import schedule


async def test_sqlite_rejects_a_case_variant_of_the_same_term(
    services: Services, user: User, session: AsyncSession
) -> None:
    """``run`` и ``Run`` — одно слово: различие регистра не создаёт вторую карточку.

    Сервис отсеивает такой дубль сам, поэтому проверяется именно база: карточка
    вставляется через репозиторий, минующий проверку терминов.
    """
    await services.card_service.add_draft(user, CardDraft(term="run", translation="бегать"))
    await services.commit()
    deck = await services.card_service.default_deck(user)

    with pytest.raises(IntegrityError):
        await services.cards.add(
            user_id=user.id,
            deck_id=deck.id,
            draft=CardDraft(term="Run", translation="бегать"),
            schedule=schedule(0),
        )
    await session.rollback()


async def test_the_term_unique_index_is_functional(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        definition = await connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND name = 'uq_cards_user_id_deck_id_term_language_direction'"
            )
        )

    assert definition is not None
    assert "lower(term)" in definition


async def test_reviews_have_a_covering_index_for_first_answers(engine: AsyncEngine) -> None:
    """Группировка первых ответов читается из индекса, не из таблицы."""

    def indexes(connection: Connection) -> dict[str | None, list[str | None]]:
        return {
            index["name"]: index["column_names"]
            for index in inspect(connection).get_indexes("reviews")
        }

    async with engine.connect() as connection:
        found = await connection.run_sync(indexes)

    assert found["ix_reviews_user_id_card_id_reviewed_at"] == [
        "user_id",
        "card_id",
        "reviewed_at",
    ]


async def test_redundant_prefix_indexes_are_gone(engine: AsyncEngine) -> None:
    """Однотипные индексы-префиксы дублируют составные и только замедляют запись."""

    async with engine.connect() as connection:
        found: dict[str, set[str]] = {}
        for table in ("cards", "reviews", "streaks", "decks"):
            rows = await connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :table"),
                {"table": table},
            )
            found[table] = {row[0] for row in rows}

    assert found["cards"].isdisjoint(
        {"ix_cards_user_id", "ix_cards_deck_id", "ix_cards_term", "ix_cards_due_at"}
    )
    assert "ix_reviews_user_id" not in found["reviews"]
    assert "ix_streaks_user_id" not in found["streaks"]
    assert "ix_decks_user_id" not in found["decks"]
