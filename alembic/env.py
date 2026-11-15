"""Alembic environment.

The database URL comes from the application settings rather than ``alembic.ini``,
so a migration can never run against a different database than the bot uses.

Concurrency: an online PostgreSQL run takes a session-level advisory lock before
touching the schema and releases it in ``finally``. A deploy can overlap with the
one-shot ``migrate`` service being restarted, and two ``alembic upgrade`` runs on
one database interleave their DDL until the version table no longer matches
reality. PostgreSQL also drops the lock when the connection dies, so a killed
migration never leaves the database locked; SQLite has no advisory locks — nor
concurrent writers worth guarding against — so local development skips them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from logging.config import fileConfig
from typing import Any, Literal

import sqlalchemy as sa
from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

import eng_remember.models  # noqa: F401  — registers every table on Base.metadata
from eng_remember.config import get_settings
from eng_remember.db.base import Base, UtcDateTime

log = logging.getLogger("alembic.env")

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# A name this project agrees on rather than a resource id: any fixed 64-bit
# number would do. Spelled out as ASCII "eng_rem" so a lock left behind by a
# killed process is recognisable in ``pg_locks``.
MIGRATION_LOCK_KEY = 0x656E675F72656D

target_metadata = Base.metadata

# Helps autogenerate notice a changed column type or default instead of
# silently producing an empty migration.
_COMPARE_OPTS = {
    "compare_type": True,
    "compare_server_default": True,
}


def _database_url() -> str:
    return get_settings().database_url


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | Literal[False]:
    """Render project-specific column types as plain SQLAlchemy ones.

    By default alembic writes the fully qualified class path, which would make
    every migration import this project's code — so renaming or moving
    ``UtcDateTime`` would break migrations that already ran. Emitting the
    underlying SQL type instead keeps each revision self-contained.

    Returning ``False`` means "use the default rendering". Returning ``None``
    does not: alembic takes it as the rendered value and silently drops the
    column, which is how an empty ``create_table`` gets generated.
    """
    if type_ == "type" and isinstance(obj, UtcDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _configure(connection: Connection | None = None) -> None:
    url = _database_url()
    options: dict[str, object] = {
        "target_metadata": target_metadata,
        "include_schemas": False,
        "render_item": render_item,
        # One transaction per revision instead of one long one: a failure in
        # revision N leaves 1..N-1 applied, which is what the version table
        # would claim anyway, and a long multi-revision run does not hold locks
        # over every table at once.
        "transaction_per_migration": True,
        **_COMPARE_OPTS,
    }
    if connection is not None:
        options["connection"] = connection
    else:
        options["url"] = url
        options["literal_binds"] = True
        options["dialect_opts"] = {"paramstyle": "named"}

    if url.startswith("sqlite"):
        # SQLite cannot ALTER most things in place; batch mode rewrites the
        # table instead. Keeps local development migrations working.
        options["render_as_batch"] = True

    context.configure(**options)  # type: ignore[arg-type]


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    _configure()
    with context.begin_transaction():
        context.run_migrations()


def _advisory(connection: Connection, function: str) -> None:
    """Take or release the migration lock, ending the implicit transaction.

    The lock belongs to the session, not the transaction, so the commit here
    only closes the transaction ``execute`` opened by itself. Without it the
    first revision would inherit that transaction and commit through alembic's
    proxy, leaving the version table updated one revision late.
    """
    connection.execute(sa.text(f"SELECT {function}(:key)"), {"key": MIGRATION_LOCK_KEY})
    connection.commit()


def do_run_migrations(connection: Connection) -> None:
    # PostgreSQL only: SQLite has neither advisory locks nor concurrent writers.
    locked = connection.dialect.name == "postgresql"
    if locked:
        log.info("migration_lock_waiting", extra={"key": MIGRATION_LOCK_KEY})
        _advisory(connection, "pg_advisory_lock")
        log.info("migration_lock_acquired")

    try:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if locked:
            # Best effort: if the connection died mid-migration PostgreSQL has
            # already dropped the lock, and an unlock error must not hide the
            # migration error that brought us here.
            with contextlib.suppress(Exception):
                _advisory(connection, "pg_advisory_unlock")


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
