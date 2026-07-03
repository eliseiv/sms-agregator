"""One-off on-demand sync входящих номеров Twilio-аккаунта → unassigned (ADR-0013).

Тот же механизм, что ``POST /api/admin/numbers/sync`` (общая сервис-функция
:func:`app.application.twilio_sync_service.sync_twilio_numbers_to_pool`): тянет
все входящие номера аккаунта через Twilio REST (пагинация), upsert'ит в
``phone_numbers`` как unassigned (``team_id=NULL``, ``added_by_user_id=NULL``)
идемпотентно ``ON CONFLICT (phone_number) DO NOTHING``. Аудита нет (нет актора).

Аутентификация в Twilio — ``TWILIO_ACCOUNT_SID``/``TWILIO_AUTH_TOKEN`` из
окружения. Запуск на сервере::

    docker compose run --rm app python -m scripts.sync_twilio_numbers \
        --database-url "$DATABASE_URL"

Идемпотентен: повторный прогон не создаёт дублей и не трогает назначенные
командам номера. Печатает счётчики ``synced_total / added / skipped_existing``.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.application.twilio_sync_service import (
    SyncResult,
    sync_twilio_numbers_to_pool,
)
from app.infrastructure.twilio_numbers import (
    TwilioNotConfiguredError,
    TwilioNumbersApiError,
    get_twilio_numbers_client,
)
from shared.config import get_settings


async def _run(database_url: str) -> SyncResult:
    client = get_twilio_numbers_client()
    engine = create_async_engine(database_url, pool_pre_ping=True, future=True)
    factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, class_=AsyncSession
    )
    try:
        async with factory() as session:
            return await sync_twilio_numbers_to_pool(session, client)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="On-demand sync номеров Twilio → unassigned-пул (ADR-0013)"
    )
    parser.add_argument(
        "--database-url",
        default=get_settings().DATABASE_URL,
        help="URL PostgreSQL (по умолчанию — из настроек)",
    )
    args = parser.parse_args()

    try:
        result = asyncio.run(_run(args.database_url))
    except TwilioNotConfiguredError as exc:
        raise SystemExit(
            "TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN не сконфигурированы"
        ) from exc
    except TwilioNumbersApiError as exc:
        raise SystemExit(f"Сбой Twilio API: {exc}") from exc

    print("=== Отчёт sync номеров Twilio ===")
    print(f"  получено из Twilio (synced_total): {result.synced_total}")
    print(f"  добавлено (added):                 {result.added}")
    print(f"  пропущено (skipped_existing):      {result.skipped_existing}")


if __name__ == "__main__":
    main()
