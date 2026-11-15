"""Async engine and session plumbing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from eng_remember.config import Settings

__all__ = ["create_engine", "create_session_factory", "normalized_database_url", "session_scope"]

SessionFactory = async_sessionmaker[AsyncSession]

# Long enough to ride out the short write bursts of a bot, short enough that a
# stuck writer turns into an error the handler can report instead of a hang.
SQLITE_BUSY_TIMEOUT_MS = 5000


def normalized_database_url(url: str) -> str:
    """The URL the async engine can actually open.

    ``sqlite://`` names the synchronous pysqlite driver; the application and
    Alembic both run on async engines, and both receive the same setting, so the
    conventional spelling is switched in exactly one place.
    """
    if url.startswith("sqlite://") and not url.startswith("sqlite+aiosqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


def _configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any) -> None:
    """Give every SQLite connection the pragmas a shared file needs.

    By default SQLite lets a writer block every reader and answers a second
    writer with an immediate "database is locked". WAL lets readers and the
    writer work at the same time, and the busy timeout makes the remaining
    collisions a short wait rather than a failed request. Only file-backed
    databases get this: ``:memory:`` has no journal mode to switch and no other
    connection to coordinate with.
    """
    cursor = dbapi_connection.cursor()
    try:
        # The busy timeout comes first: switching the journal mode may have to
        # wait for another connection, and without the timeout that wait fails
        # immediately with "database is locked" on a busy file.
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        # The journal mode lives in the database header, so it survives across
        # connections. Reading it first avoids taking the exclusive lock needed
        # to switch it on every new connection, which would make concurrent
        # sessions wait on each other for no reason.
        cursor.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the engine described by ``settings.database_url``.

    PostgreSQL is the production target; SQLite is supported so the bot and the
    test suite can run without any infrastructure.
    """
    url = make_url(normalized_database_url(settings.database_url))
    kwargs: dict[str, Any] = {"echo": settings.db_echo}
    file_sqlite = False

    if url.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in settings.database_url or not url.database:
            # A shared in-memory database only survives while one connection is
            # alive, so every session must reuse the same one.
            kwargs["poolclass"] = StaticPool
        else:
            file_sqlite = True
    else:
        # Pool settings describe PostgreSQL: the production target is the only
        # backend with a real connection pool. File SQLite keeps its own
        # defaults (and WAL handles concurrent readers instead).
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
        kwargs["pool_pre_ping"] = True

    engine = create_async_engine(url, **kwargs)
    if file_sqlite:
        event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)
    return engine


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
