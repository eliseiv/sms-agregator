"""Integration: скрипт migrate_sqlite_to_pg на синтетической service.db (docs/06 §20).

Отдельная БД ``smsdata``. Проверяем COUNT-сверку, инвариант «один лидер на
непустую команду», отсутствие member с team_id NULL, идемпотентность.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import asyncpg
import pytest

from scripts.migrate_sqlite_to_pg import migrate

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ROOT = Path(__file__).resolve().parents[2]
_DATA_DB_SA = "postgresql+asyncpg://sms:sms@localhost:63812/smsdata"
_DATA_DB_DSN = "postgresql://sms:sms@localhost:63812/smsdata"

_LEGACY_SCHEMA = """
CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT, is_active INTEGER, created_at TEXT);
CREATE TABLE telegram_users (id INTEGER PRIMARY KEY, telegram_id INTEGER, is_active INTEGER, created_at TEXT);
CREATE TABLE user_project_access (telegram_user_id INTEGER, project_id INTEGER);
CREATE TABLE twilio_numbers (id INTEGER PRIMARY KEY, phone_number TEXT, project_id INTEGER, label TEXT, is_active INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE inbound_messages (id INTEGER PRIMARY KEY, twilio_message_sid TEXT, from_number TEXT, to_number TEXT, body TEXT, project_id INTEGER, raw_payload_json TEXT, received_at TEXT);
CREATE TABLE telegram_deliveries (id INTEGER PRIMARY KEY, inbound_message_id INTEGER, telegram_user_id INTEGER, status TEXT, attempts INTEGER, last_error TEXT, sent_at TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE service_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
"""


def _build_sqlite(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute("INSERT INTO projects VALUES (1,'Proj A',1,'2026-01-01')")
    conn.execute("INSERT INTO projects VALUES (2,'Proj B',1,'2026-01-02')")  # пустая
    # tg-user 1,2 → Proj A; tg-user 3 → без доступа (orphan → Legacy)
    conn.execute("INSERT INTO telegram_users VALUES (1,1001,1,'2026-01-01')")
    conn.execute("INSERT INTO telegram_users VALUES (2,1002,1,'2026-01-01')")
    conn.execute("INSERT INTO telegram_users VALUES (3,1003,1,'2026-01-01')")
    conn.execute("INSERT INTO user_project_access VALUES (1,1)")
    conn.execute("INSERT INTO user_project_access VALUES (2,1)")
    conn.execute(
        "INSERT INTO twilio_numbers VALUES (1,'+441000000001',1,NULL,1,'2026-01-01','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO twilio_numbers VALUES (2,'+441000000002',2,NULL,1,'2026-01-01','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO inbound_messages VALUES (1,'SIDA','+1','+441000000001','hi',1,'{}','2026-02-01')"
    )
    conn.execute(
        "INSERT INTO inbound_messages VALUES (2,'SIDB','+1','+449999999999','who',NULL,'{}','2026-02-02')"
    )
    conn.execute(
        "INSERT INTO telegram_deliveries VALUES (1,1,1,'sent',1,NULL,'2026-02-01','2026-02-01','2026-02-01')"
    )
    conn.execute(
        "INSERT INTO telegram_deliveries VALUES (2,1,2,'pending',0,NULL,NULL,'2026-02-01','2026-02-01')"
    )
    conn.execute(
        "INSERT INTO service_state VALUES ('telegram_offset','5','2026-02-01')"
    )
    conn.execute("INSERT INTO service_state VALUES ('foo','bar','2026-02-01')")
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


async def _counts() -> dict[str, int]:
    conn = await asyncpg.connect(_DATA_DB_DSN)
    try:
        out = {}
        for t in (
            "teams",
            "users",
            "telegram_links",
            "phone_numbers",
            "inbound_sms",
            "deliveries",
            "service_state",
        ):
            out[t] = int(await conn.fetchval(f"SELECT count(*) FROM {t}"))
        out["null_team_members"] = int(
            await conn.fetchval(
                "SELECT count(*) FROM users WHERE team_id IS NULL "
                "AND role IN ('group_member','group_leader')"
            )
        )
        out["bad_leader_teams"] = int(
            await conn.fetchval(
                "SELECT count(*) FROM teams t WHERE EXISTS "
                "(SELECT 1 FROM users u WHERE u.team_id=t.id) AND ("
                "  (SELECT count(*) FROM users u WHERE u.team_id=t.id "
                "   AND u.role='group_leader') <> 1)"
            )
        )
        return out
    finally:
        await conn.close()


async def test_data_migration_counts_and_invariants():
    await _prepare_pg()
    with tempfile.TemporaryDirectory() as d:
        sqlite_path = str(Path(d) / "service.db")
        _build_sqlite(sqlite_path)

        report = await migrate(sqlite_path, _DATA_DB_SA, "Legacy")
        assert report["orphan_users"] == 1
        c1 = await _counts()

        # COUNT-сверка.
        assert c1["teams"] == 3  # 2 проекта + Legacy
        assert c1["users"] == 3
        assert c1["telegram_links"] == 3
        assert c1["phone_numbers"] == 2
        assert c1["inbound_sms"] == 2
        assert c1["deliveries"] == 2
        assert c1["service_state"] == 1  # telegram_offset пропущен
        # Инварианты.
        assert c1["null_team_members"] == 0
        assert c1["bad_leader_teams"] == 0

        # Идемпотентность: повторный прогон без дублей.
        report2 = await migrate(sqlite_path, _DATA_DB_SA, "Legacy")
        assert all(
            report2[k] == 0
            for k in ("teams", "users", "phone_numbers", "inbound_sms", "deliveries")
        )
        c2 = await _counts()
        assert c2 == c1
