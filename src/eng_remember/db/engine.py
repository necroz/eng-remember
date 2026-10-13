"""Async engine and session plumbing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from eng_remember.config import Settings

__all__ = ["create_engine", "create_session_factory", "session_scope"]

SessionFactory = async_sessionmaker[AsyncSession]


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the engine described by ``settings.database_url``.

    PostgreSQL is the production target; SQLite is supported so the bot and the
    test suite can run without any infrastructure.
    """
    url = make_url(settings.database_url)
    kwargs: dict[str, Any] = {"echo": settings.db_echo}

    if url.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in settings.database_url or not url.database:
            # A shared in-memory database only survives while one connection is
            # alive, so every session must reuse the same one.
            kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
        kwargs["pool_pre_ping"] = True

    return create_async_engine(settings.database_url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Session with an explicit commit/rollback boundary.

    Used by scripts and tests. Inside the bot the transaction boundary is owned
    by the ``DbSessionMiddleware``, which uses the same shape.
    """
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
