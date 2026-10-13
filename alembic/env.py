"""Alembic environment.

The database URL comes from the application settings rather than ``alembic.ini``,
so a migration can never run against a different database than the bot uses.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

import eng_remember.models  # noqa: F401  — registers every table on Base.metadata
from eng_remember.config import get_settings
from eng_remember.db.base import Base, UtcDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

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


def do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


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
