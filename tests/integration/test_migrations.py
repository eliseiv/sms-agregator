"""Integration: обратимость миграций + пустой autogenerate-diff (docs/06 §Схема 1,2).

Использует отдельную БД ``smsmig``, чтобы не трогать основную тест-БД.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ROOT = Path(__file__).resolve().parents[2]
_MIG_DB = "postgresql+asyncpg://sms:sms@localhost:55620/smsmig"


def _alembic(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


async def test_upgrade_downgrade_upgrade_reversible():
    up1 = _alembic("upgrade", "head", database_url=_MIG_DB)
    assert up1.returncode == 0, up1.stderr
    down = _alembic("downgrade", "base", database_url=_MIG_DB)
    assert down.returncode == 0, down.stderr
    up2 = _alembic("upgrade", "head", database_url=_MIG_DB)
    assert up2.returncode == 0, up2.stderr


async def test_autogenerate_diff_is_empty():
    """Модели ↔ применённая схема согласованы (пустой autogenerate-diff).

    Используем in-process ``compare_metadata`` вместо ``alembic revision``
    (последний требует ``script.py.mako``). Триггеры/функции не входят в
    metadata SQLAlchemy и здесь не сравниваются — их наличие проверяет
    ``test_schema``.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy.ext.asyncio import create_async_engine

    import shared.models  # noqa: F401  (регистрирует таблицы)
    from shared.db import Base

    _alembic("upgrade", "head", database_url=_MIG_DB)

    engine = create_async_engine(_MIG_DB)
    try:
        async with engine.connect() as conn:
            diffs = await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(sync_conn, opts={"compare_type": True}),
                    Base.metadata,
                )
            )
    finally:
        await engine.dispose()

    # Отфильтровать возможный «шум» по служебной таблице alembic_version.
    meaningful = [d for d in diffs if "alembic_version" not in repr(d)]
    assert meaningful == [], f"autogenerate diff не пуст: {meaningful}"
