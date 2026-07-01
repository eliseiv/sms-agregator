"""One-off идемпотентная миграция данных SQLite → PostgreSQL (ADR-0006).

Запуск ПОСЛЕ ``alembic upgrade head``::

    python -m scripts.migrate_sqlite_to_pg --sqlite <path> \
        --database-url <postgresql+asyncpg://...> --orphan-team-name Legacy

Сохраняет исходные id (для совпадения FK), в конце — ``setval`` sequences,
вставки ``ON CONFLICT DO NOTHING`` (повторный прогон безопасен). Печатает отчёт.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import asyncpg  # type: ignore[import-untyped]

from shared.config import get_settings


def _to_dsn(database_url: str) -> str:
    """Привести SQLAlchemy-URL к asyncpg-DSN."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(UTC)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _open_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


async def _get_or_create_legacy_team(conn: asyncpg.Connection, name: str) -> int:
    row = await conn.fetchrow("SELECT id FROM teams WHERE name = $1", name)
    if row is not None:
        return int(row["id"])
    row = await conn.fetchrow(
        "INSERT INTO teams (name, leader_user_id) VALUES ($1, NULL) RETURNING id", name
    )
    return int(row["id"])


async def migrate(
    sqlite_path: str, database_url: str, orphan_team_name: str
) -> dict[str, int]:
    report: dict[str, int] = {
        "teams": 0,
        "users": 0,
        "telegram_links": 0,
        "phone_numbers": 0,
        "inbound_sms": 0,
        "deliveries": 0,
        "service_state": 0,
        "orphan_users": 0,
        "multi_project_users": 0,
    }
    sq = _open_sqlite(sqlite_path)
    pg = await asyncpg.connect(_to_dsn(database_url))
    try:
        async with pg.transaction():
            # 1. projects → teams (сохраняем id; leader позже).
            for proj in sq.execute("SELECT * FROM projects").fetchall():
                res = await pg.execute(
                    "INSERT INTO teams (id, name, is_active, created_at) "
                    "VALUES ($1, $2, $3, $4) ON CONFLICT (id) DO NOTHING",
                    int(proj["id"]),
                    str(proj["name"]),
                    bool(proj["is_active"]),
                    _parse_dt(proj["created_at"]),
                )
                report["teams"] += 1 if res.endswith("1") else 0

            # Продвинуть sequence teams.id ДО автогенерации Legacy-команды,
            # иначе nextval=1 столкнётся с id проектов (BUG-2, teams_pkey).
            await pg.execute(
                "SELECT setval(pg_get_serial_sequence('teams', 'id'), "
                "COALESCE((SELECT MAX(id) FROM teams), 1), true)"
            )

            # 2. telegram_users → users + telegram_links.
            legacy_team_id: int | None = None
            for tu in sq.execute("SELECT * FROM telegram_users").fetchall():
                old_id = int(tu["id"])
                telegram_id = int(tu["telegram_id"])
                access = sq.execute(
                    "SELECT project_id FROM user_project_access "
                    "WHERE telegram_user_id = ? ORDER BY project_id",
                    (old_id,),
                ).fetchall()
                project_ids = [int(r["project_id"]) for r in access]
                if not project_ids:
                    if legacy_team_id is None:
                        legacy_team_id = await _get_or_create_legacy_team(
                            pg, orphan_team_name
                        )
                    team_id = legacy_team_id
                    report["orphan_users"] += 1
                else:
                    team_id = project_ids[0]
                    if len(project_ids) > 1:
                        report["multi_project_users"] += 1  # TD-004

                username = f"tg_{telegram_id}"
                res = await pg.execute(
                    "INSERT INTO users (id, username, password_hash, role, team_id, "
                    "password_reset_required, created_at) "
                    "VALUES ($1, $2, NULL, 'group_member', $3, true, $4) "
                    "ON CONFLICT (id) DO NOTHING",
                    old_id,
                    username,
                    team_id,
                    _parse_dt(tu["created_at"]),
                )
                report["users"] += 1 if res.endswith("1") else 0

                dead_at = None if bool(tu["is_active"]) else datetime.now(UTC)
                res = await pg.execute(
                    "INSERT INTO telegram_links (telegram_user_id, user_id, created_at, dead_at) "
                    "VALUES ($1, $2, $3, $4) ON CONFLICT (telegram_user_id) DO NOTHING",
                    telegram_id,
                    old_id,
                    _parse_dt(tu["created_at"]),
                    dead_at,
                )
                report["telegram_links"] += 1 if res.endswith("1") else 0

            # 3. Лидеры: leader = min(user_id) среди участников команды.
            team_rows = await pg.fetch("SELECT id FROM teams")
            for trow in team_rows:
                team_id = int(trow["id"])
                leader = await pg.fetchrow(
                    "SELECT id FROM users WHERE team_id = $1 ORDER BY id LIMIT 1",
                    team_id,
                )
                if leader is None:
                    continue
                leader_id = int(leader["id"])
                await pg.execute(
                    "UPDATE users SET role = 'group_leader' WHERE id = $1", leader_id
                )
                await pg.execute(
                    "UPDATE teams SET leader_user_id = $1 WHERE id = $2 "
                    "AND leader_user_id IS NULL",
                    leader_id,
                    team_id,
                )

            # 4. twilio_numbers → phone_numbers.
            for num in sq.execute("SELECT * FROM twilio_numbers").fetchall():
                team_id = int(num["project_id"])
                leader = await pg.fetchrow(
                    "SELECT leader_user_id FROM teams WHERE id = $1", team_id
                )
                added_by = (
                    int(leader["leader_user_id"])
                    if leader and leader["leader_user_id"]
                    else None
                )
                res = await pg.execute(
                    "INSERT INTO phone_numbers (id, phone_number, team_id, added_by_user_id, "
                    "label, is_active, created_at, updated_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (id) DO NOTHING",
                    int(num["id"]),
                    str(num["phone_number"]),
                    team_id,
                    added_by,
                    num["label"],
                    bool(num["is_active"]),
                    _parse_dt(num["created_at"]),
                    _parse_dt(num["updated_at"]),
                )
                report["phone_numbers"] += 1 if res.endswith("1") else 0

            # 5. inbound_messages → inbound_sms.
            for msg in sq.execute("SELECT * FROM inbound_messages").fetchall():
                msg_team_id = (
                    int(msg["project_id"]) if msg["project_id"] is not None else None
                )
                raw = msg["raw_payload_json"] or "{}"
                try:
                    json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    raw = "{}"
                res = await pg.execute(
                    "INSERT INTO inbound_sms (id, twilio_message_sid, from_number, to_number, "
                    "body, team_id, raw_payload, received_at, created_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $8) "
                    "ON CONFLICT (id) DO NOTHING",
                    int(msg["id"]),
                    msg["twilio_message_sid"],
                    str(msg["from_number"]),
                    str(msg["to_number"]),
                    str(msg["body"]),
                    msg_team_id,
                    raw,
                    _parse_dt(msg["received_at"]),
                )
                report["inbound_sms"] += 1 if res.endswith("1") else 0

            # 6. telegram_deliveries → deliveries (map old tg_user.id → user_id + chat_id).
            for dlv in sq.execute("SELECT * FROM telegram_deliveries").fetchall():
                old_user_id = int(dlv["telegram_user_id"])  # FK на telegram_users.id
                tu = sq.execute(
                    "SELECT telegram_id FROM telegram_users WHERE id = ?",
                    (old_user_id,),
                ).fetchone()
                if tu is None:
                    continue
                chat_id = int(tu["telegram_id"])
                res = await pg.execute(
                    "INSERT INTO deliveries (id, inbound_sms_id, user_id, telegram_user_id, "
                    "status, attempts, last_error, sent_at, created_at, updated_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
                    "ON CONFLICT (inbound_sms_id, telegram_user_id) DO NOTHING",
                    int(dlv["id"]),
                    int(dlv["inbound_message_id"]),
                    old_user_id,
                    chat_id,
                    str(dlv["status"])
                    if dlv["status"] in ("pending", "sent", "failed", "dead")
                    else "failed",
                    int(dlv["attempts"] or 0),
                    dlv["last_error"],
                    _parse_dt(dlv["sent_at"]) if dlv["sent_at"] else None,
                    _parse_dt(dlv["created_at"]),
                    _parse_dt(dlv["updated_at"]),
                )
                report["deliveries"] += 1 if res.endswith("1") else 0

            # 7. service_state (кроме telegram_offset).
            for st in sq.execute("SELECT * FROM service_state").fetchall():
                if str(st["key"]) == "telegram_offset":
                    continue
                res = await pg.execute(
                    "INSERT INTO service_state (key, value, updated_at) "
                    "VALUES ($1, $2, $3) ON CONFLICT (key) DO NOTHING",
                    str(st["key"]),
                    str(st["value"]),
                    _parse_dt(st["updated_at"]),
                )
                report["service_state"] += 1 if res.endswith("1") else 0

            # setval sequences (id-колонки BIGSERIAL).
            for table in (
                "teams",
                "users",
                "phone_numbers",
                "inbound_sms",
                "deliveries",
            ):
                await pg.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1), true)"
                )
    finally:
        await pg.close()
        sq.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Миграция данных SQLite → PostgreSQL (ADR-0006)"
    )
    parser.add_argument("--sqlite", required=True, help="Путь к service.db (SQLite)")
    parser.add_argument(
        "--database-url",
        default=get_settings().DATABASE_URL,
        help="URL PostgreSQL (по умолчанию — из настроек)",
    )
    parser.add_argument(
        "--orphan-team-name", default="Legacy", help="Имя служебной команды"
    )
    args = parser.parse_args()

    report = asyncio.run(migrate(args.sqlite, args.database_url, args.orphan_team_name))
    print("=== Отчёт миграции ===")
    for key, value in report.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
