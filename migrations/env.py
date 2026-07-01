"""Alembic env (async-first).

Использует asyncpg-engine; ``run_sync`` мостит миграции на sync-соединение,
спроецированное из async.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import shared.models  # noqa: F401 — регистрирует ORM-таблицы на Base.metadata
from shared.config import get_settings
from shared.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _async_database_url() -> str:
    return get_settings().DATABASE_URL


config.set_main_option("sqlalchemy.url", _async_database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=_async_database_url().replace("postgresql+asyncpg://", "postgresql://"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(_async_database_url(), poolclass=None)
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
