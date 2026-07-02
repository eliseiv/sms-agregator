"""Integration: scripts/import_numbers — импорт номеров как unassigned (docs/06 §7, ADR-0009)."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import asyncpg
import pytest

from scripts.import_numbers import import_numbers
from tests.conftest import sibling_dsn, sibling_sa_url

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ROOT = Path(__file__).resolve().parents[2]
_DATA_DB_SA = sibling_sa_url("smsdata")
_DATA_DB_DSN = sibling_dsn("smsdata")


def _build_sqlite(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE twilio_numbers (id INTEGER PRIMARY KEY, phone_number TEXT, "
        "project_id INTEGER, label TEXT, is_active INTEGER, created_at TEXT, updated_at TEXT);"
        "CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT);"
    )
    conn.execute(
        "INSERT INTO twilio_numbers VALUES (1,'+441270000001',1,'касса',1,'x','x')"
    )
    conn.execute(
        "INSERT INTO twilio_numbers VALUES (2,'+441270000002',1,NULL,1,'x','x')"
    )
    conn.execute("INSERT INTO projects VALUES (1,'Legacy Proj')")
    conn.commit()
    conn.close()


async def _prepare_pg() -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = _DATA_DB_SA
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    conn = await asyncpg.connect(_DATA_DB_DSN)
    try:
        await conn.execute(
            "TRUNCATE deliveries, inbound_sms, phone_numbers, telegram_links, "
            "admin_audit, service_state, users, teams RESTART IDENTITY CASCADE"
        )
    finally:
        await conn.close()


async def _pg_val(sql: str):
    conn = await asyncpg.connect(_DATA_DB_DSN)
    try:
        return await conn.fetchval(sql)
    finally:
        await conn.close()


async def test_import_numbers_as_unassigned_idempotent():
    await _prepare_pg()
    with tempfile.TemporaryDirectory() as d:
        sqlite_path = str(Path(d) / "service.db")
        _build_sqlite(sqlite_path)

        report = await import_numbers(sqlite_path, _DATA_DB_SA)
        assert report["read"] == 2
        assert report["inserted"] == 2
        assert report["skipped"] == 0

        # Все номера — unassigned (team_id / added_by_user_id = NULL).
        assert await _pg_val("SELECT count(*) FROM phone_numbers") == 2
        assert (
            await _pg_val("SELECT count(*) FROM phone_numbers WHERE team_id IS NULL")
            == 2
        )
        assert (
            await _pg_val(
                "SELECT count(*) FROM phone_numbers WHERE added_by_user_id IS NULL"
            )
            == 2
        )
        # projects/teams/users не затронуты.
        assert await _pg_val("SELECT count(*) FROM teams") == 0
        assert await _pg_val("SELECT count(*) FROM users") == 0

        # Повторный прогон — без дублей (ON CONFLICT DO NOTHING).
        report2 = await import_numbers(sqlite_path, _DATA_DB_SA)
        assert report2["read"] == 2
        assert report2["inserted"] == 0
        assert report2["skipped"] == 2
        assert await _pg_val("SELECT count(*) FROM phone_numbers") == 2
