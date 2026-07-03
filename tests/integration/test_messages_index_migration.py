"""Integration: обратимость индекса ix_inbound_sms_to_number_received (docs/06 §10).

Ревизия 20260703_004 (ADR-0014): upgrade создаёт композитный индекс
``inbound_sms (to_number, received_at DESC, id DESC)``; downgrade его удаляет.
Прогон на отдельной БД ``smsmig`` (как test_migrations); БД возвращается в head.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

from tests.conftest import sibling_dsn, sibling_sa_url

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ROOT = Path(__file__).resolve().parents[2]
_MIG_DB = sibling_sa_url("smsmig")
_MIG_DSN = sibling_dsn("smsmig")
_INDEX = "ix_inbound_sms_to_number_received"
_PREV_REVISION = "20260702_003"


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["DATABASE_URL"] = _MIG_DB
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


async def _index_exists() -> bool:
    conn = await asyncpg.connect(_MIG_DSN)
    try:
        row = await conn.fetchval(
            "SELECT count(*) FROM pg_indexes "
            "WHERE schemaname='public' AND indexname=$1",
            _INDEX,
        )
    finally:
        await conn.close()
    return int(row) == 1


async def _index_columns_desc() -> str:
    """Определение индекса (для проверки порядка колонок to_number, DESC keyset)."""
    conn = await asyncpg.connect(_MIG_DSN)
    try:
        return await conn.fetchval(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname='public' AND indexname=$1",
            _INDEX,
        )
    finally:
        await conn.close()


async def test_index_upgrade_creates_downgrade_drops():
    # Гарантируем актуальное состояние.
    up0 = _alembic("upgrade", "head")
    assert up0.returncode == 0, up0.stderr
    assert await _index_exists(), "индекс не создан после upgrade head"

    indexdef = await _index_columns_desc()
    # Композитный индекс с DESC-порядком и tie-break по id.
    assert "to_number" in indexdef
    assert "received_at" in indexdef
    assert "id" in indexdef
    assert "DESC" in indexdef

    # downgrade на ревизию ДО 004 → индекс удалён.
    down = _alembic("downgrade", _PREV_REVISION)
    assert down.returncode == 0, down.stderr
    assert not await _index_exists(), "индекс не удалён после downgrade"

    # Восстановить head → индекс снова присутствует (и БД возвращена в head).
    up1 = _alembic("upgrade", "head")
    assert up1.returncode == 0, up1.stderr
    assert await _index_exists(), "индекс не восстановлен после повторного upgrade"
