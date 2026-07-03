"""Integration: обратимость миграций + пустой autogenerate-diff (docs/06 §Схема 1,2).

Использует отдельную БД ``smsmig``, чтобы не трогать основную тест-БД.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import asyncpg

from tests.conftest import sibling_dsn, sibling_sa_url

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ROOT = Path(__file__).resolve().parents[2]
_MIG_DB = sibling_sa_url("smsmig")
_MIG_DSN = sibling_dsn("smsmig")


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


async def test_003_backfill_home_memberships():
    """Ревизия 003: backfill user_teams = число не-super_admin с team_id (docs/06 §32)."""
    # Привести smsmig к состоянию ДО 003 и засеять legacy-данные.
    down = _alembic("downgrade", "20260702_002", database_url=_MIG_DB)
    assert down.returncode == 0, down.stderr

    conn = await asyncpg.connect(_MIG_DSN)
    try:
        # Одна транзакция: DEFERRABLE-триггер лидерства проверяется при commit,
        # когда users и teams.leader_user_id уже согласованы.
        async with conn.transaction():
            await conn.execute("TRUNCATE users, teams RESTART IDENTITY CASCADE")
            await conn.execute("INSERT INTO teams (id, name) VALUES (1,'T1'),(2,'T2')")
            # 3 не-super_admin с team_id + 1 super_admin без team_id.
            await conn.execute(
                "INSERT INTO users (id, username, role, team_id) VALUES "
                "(1,'sa','super_admin',NULL),"
                "(2,'u1','group_leader',1),"
                "(3,'u2','group_member',1),"
                "(4,'u3','group_leader',2)"
            )
            await conn.execute("UPDATE teams SET leader_user_id=2 WHERE id=1")
            await conn.execute("UPDATE teams SET leader_user_id=4 WHERE id=2")
    finally:
        await conn.close()

    up = _alembic("upgrade", "head", database_url=_MIG_DB)
    assert up.returncode == 0, up.stderr

    conn = await asyncpg.connect(_MIG_DSN)
    try:
        ut = await conn.fetchval("SELECT count(*) FROM user_teams")
        expected = await conn.fetchval(
            "SELECT count(*) FROM users WHERE team_id IS NOT NULL AND role <> 'super_admin'"
        )
        # У каждого не-super_admin с team_id есть строка home-членства.
        pairs = await conn.fetchval(
            "SELECT count(*) FROM users u JOIN user_teams ut "
            "ON ut.user_id=u.id AND ut.team_id=u.team_id "
            "WHERE u.team_id IS NOT NULL"
        )
        sa_rows = await conn.fetchval("SELECT count(*) FROM user_teams WHERE user_id=1")
    finally:
        await conn.close()
    assert ut == expected == 3
    assert pairs == 3
    assert sa_rows == 0  # super_admin не попал в backfill
