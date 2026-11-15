from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.models.deck import Deck

__all__ = ["DeckRepository"]


class DeckRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, deck_id: int, *, user_id: int) -> Deck | None:
        """Fetch a deck, scoped to its owner.

        Ownership is part of the lookup rather than a separate check so a stale
        callback button can never reach another user's data.
        """
        stmt = select(Deck).where(Deck.id == deck_id, Deck.user_id == user_id)
        return await self._session.scalar(stmt)

    async def get_by_name(self, user_id: int, name: str) -> Deck | None:
        stmt = select(Deck).where(Deck.user_id == user_id, Deck.name == name)
        return await self._session.scalar(stmt)

    async def get_default(self, user_id: int) -> Deck | None:
        stmt = select(Deck).where(Deck.user_id == user_id, Deck.is_default.is_(True))
        return await self._session.scalar(stmt)

    async def get_or_create_default(self, user_id: int, *, name: str) -> Deck:
        existing = await self.get_default(user_id)
        if existing is not None:
            return existing

        by_name = await self.get_by_name(user_id, name)
        if by_name is not None:
            by_name.is_default = True
            return by_name

        # Two first updates of the same account can both reach this point. Let
        # the unique ``(user_id, name)`` index arbitrate instead of raising; the
        # loser reads the winner's deck back and only has to make it default.
        dialect_insert = (
            sqlite_insert
            if self._session.get_bind().dialect.name == "sqlite"
            else postgresql_insert
        )
        statement = (
            dialect_insert(Deck)
            .values(user_id=user_id, name=name, is_default=True)
            .on_conflict_do_nothing(index_elements=["user_id", "name"])
        )
        await self._session.execute(statement)

        deck = await self.get_by_name(user_id, name)
        if deck is None:  # pragma: no cover — the row was just inserted or read back
            raise RuntimeError(f"default deck {name!r} vanished after upsert")
        if not deck.is_default:
            deck.is_default = True
        return deck

    async def create(
        self,
        user_id: int,
        name: str,
        *,
        description: str | None = None,
        is_default: bool = False,
    ) -> Deck:
        deck = Deck(user_id=user_id, name=name, description=description, is_default=is_default)
        self._session.add(deck)
        await self._session.flush()
        return deck

    async def set_topic(self, deck: Deck, topic: str | None) -> Deck:
        """Remember what the deck is currently about.

        Read back when the learner asks for "more words" without naming a
        subject — the last context they worked in is the best guess available.
        """
        deck.topic = (topic or "").strip() or None
        await self._session.flush()
        return deck

    async def list_for_user(self, user_id: int) -> list[Deck]:
        stmt = (
            select(Deck).where(Deck.user_id == user_id).order_by(Deck.is_default.desc(), Deck.name)
        )
        return list((await self._session.scalars(stmt)).all())

    async def count_for_user(self, user_id: int) -> int:
        stmt = select(func.count(Deck.id)).where(Deck.user_id == user_id)
        return int(await self._session.scalar(stmt) or 0)
