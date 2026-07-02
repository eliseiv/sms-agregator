"""One-off идемпотентный импорт номеров SQLite → PostgreSQL как unassigned (ADR-0009).

Переносит из старой SQLite (``twilio_numbers``: ``phone_number``, ``label``,
``is_active``) в ``phone_numbers`` как unassigned (``team_id = NULL``,
``added_by_user_id = NULL``). projects/teams/users/deliveries НЕ переносятся —
это независимо от полной миграции (ADR-0006).

Запуск ПОСЛЕ ``alembic upgrade head`` (нужна ревизия team_id NULLABLE)::

    python -m scripts.import_numbers --sqlite <path> \
        --database-url <postgresql+asyncpg://...>

Идемпотентность — ``INSERT ... ON CONFLICT (phone_number) DO NOTHING``: повторный
прогон не создаёт дублей и не перезаписывает уже назначенные номера. Печатает
отчёт: прочитано / вставлено / пропущено (конфликт).
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3

import asyncpg  # type: ignore[import-untyped]

from shared.config import get_settings


def _to_dsn(database_url: str) -> str:
    """Привести SQLAlchemy-URL к asyncpg-DSN."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _open_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


async def import_numbers(sqlite_path: str, database_url: str) -> dict[str, int]:
    report: dict[str, int] = {"read": 0, "inserted": 0, "skipped": 0}
    sq = _open_sqlite(sqlite_path)
    pg = await asyncpg.connect(_to_dsn(database_url))
    try:
        async with pg.transaction():
            for num in sq.execute("SELECT * FROM twilio_numbers").fetchall():
                report["read"] += 1
                phone_number = str(num["phone_number"])
                res = await pg.execute(
                    "INSERT INTO phone_numbers "
                    "(phone_number, team_id, added_by_user_id, label, is_active) "
                    "VALUES ($1, NULL, NULL, $2, $3) "
                    "ON CONFLICT (phone_number) DO NOTHING",
                    phone_number,
                    num["label"],
                    bool(num["is_active"]),
                )
                if res.endswith("1"):
                    report["inserted"] += 1
                else:
                    report["skipped"] += 1
    finally:
        await pg.close()
        sq.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Импорт номеров SQLite → PostgreSQL как unassigned (ADR-0009)"
    )
    parser.add_argument("--sqlite", required=True, help="Путь к service.db (SQLite)")
    parser.add_argument(
        "--database-url",
        default=get_settings().DATABASE_URL,
        help="URL PostgreSQL (по умолчанию — из настроек)",
    )
    args = parser.parse_args()

    report = asyncio.run(import_numbers(args.sqlite, args.database_url))
    print("=== Отчёт импорта номеров ===")
    print(f"  прочитано: {report['read']}")
    print(f"  вставлено: {report['inserted']}")
    print(f"  пропущено (конфликт): {report['skipped']}")


if __name__ == "__main__":
    main()
