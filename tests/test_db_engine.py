"""Engine settings that the test suite never noticed.

SQLite is the zero-infrastructure target, but its defaults are hostile to
concurrent sessions: readers block writers and a second writer gets an immediate
"database is locked" instead of waiting its turn. The pragmas below are what
makes the file-backed database usable for a bot that opens several sessions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.pool import QueuePool, StaticPool

from eng_remember.config import Settings
from eng_remember.db import create_engine
from eng_remember.db.engine import normalized_database_url


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_sqlite_url_normalisation_is_shared_with_alembic() -> None:
    """The app and the migration env switch the driver in one place."""
    assert normalized_database_url("sqlite:///data.db") == "sqlite+aiosqlite:///data.db"
    assert normalized_database_url("sqlite+aiosqlite:///data.db") == "sqlite+aiosqlite:///data.db"
    assert (
        normalized_database_url("postgresql+asyncpg://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    )


async def test_file_sqlite_uses_wal(tmp_path: Path) -> None:
    database = tmp_path / "wal.db"
    engine = create_engine(make_settings(database_url=f"sqlite+aiosqlite:///{database}"))
    try:
        async with engine.connect() as connection:
            journal_mode = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar()
            busy_timeout = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar()
    finally:
        await engine.dispose()

    assert journal_mode == "wal"
    assert busy_timeout == 5000


async def test_conventional_sqlite_url_gets_the_async_driver(tmp_path: Path) -> None:
    """``sqlite://`` is the spelling operators know; the engine must still await.

    SQLAlchemy would hand ``sqlite://`` to pysqlite, which the async engine
    rejects outright, so the engine swaps in the aiosqlite driver itself.
    """
    database = tmp_path / "sync-spelling.db"
    engine = create_engine(make_settings(database_url=f"sqlite:///{database}"))
    try:
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql("select 1")).scalar() == 1
            journal_mode = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar()
    finally:
        await engine.dispose()

    assert journal_mode == "wal"


async def test_in_memory_sqlite_keeps_the_pragmas_out() -> None:
    """``:memory:`` has no WAL journal and no other writer to wait for.

    It also has no file to share, so the engine must keep every session on one
    connection (``StaticPool``) rather than hand out fresh empty databases.
    """
    engine = create_engine(make_settings(database_url="sqlite+aiosqlite:///:memory:"))
    try:
        async with engine.connect() as connection:
            journal_mode = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar()
    finally:
        await engine.dispose()

    assert journal_mode == "memory"
    assert isinstance(engine.pool, StaticPool)


async def test_postgres_engine_uses_the_configured_pool() -> None:
    """The settings reach the pool, not just the config object.

    No connection is opened: building the pool is enough to read back its shape,
    so this stays a unit test without a PostgreSQL instance.
    """
    engine = create_engine(
        make_settings(
            database_url="postgresql+asyncpg://eng:eng@localhost:55432/eng_remember",
            db_pool_size=12,
            db_max_overflow=7,
        )
    )
    try:
        pool = engine.pool
        assert isinstance(pool, QueuePool)
        assert pool.size() == 12
        assert pool._max_overflow == 7
    finally:
        await engine.dispose()


@pytest.mark.parametrize("size", [0, -1])
def test_pool_size_must_be_positive(size: int) -> None:
    with pytest.raises(ValueError, match="db_pool_size"):
        make_settings(db_pool_size=size)
