"""On-demand sync входящих номеров Twilio → unassigned-пул (ADR-0013).

Единый механизм для HTTP-endpoint (``POST /api/admin/numbers/sync``) и CLI
(``scripts/sync_twilio_numbers.py``): тянет все номера аккаунта (пагинация),
нормализует, идемпотентно upsert'ит как unassigned. Аудит — опциональный хук,
выполняемый в той же транзакции (только для HTTP-пути; у CLI актора нет).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.services import normalize_phone
from app.infrastructure.repositories import PhoneNumberRepository
from app.infrastructure.twilio_numbers import (
    TwilioNotConfiguredError,
    TwilioNumbersClient,
)
from shared.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SyncResult:
    synced_total: int
    added: int
    skipped_existing: int


def _dedupe_normalize(
    numbers: list[tuple[str, str | None]],
) -> list[tuple[str, str | None]]:
    """Нормализовать (E.164) и дедуплицировать по номеру, сохраняя первый label.

    Дедупликация до вставки исключает конфликт одинаковых ключей внутри одного
    ``INSERT``; отброшенные дубли/пустые попадают в ``skipped_existing`` (счётчик
    считается от исходного ``synced_total``, а не от размера батча).
    """
    seen: set[str] = set()
    result: list[tuple[str, str | None]] = []
    for phone, label in numbers:
        normalized = normalize_phone(phone)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append((normalized, label))
    return result


async def sync_twilio_numbers_to_pool(
    session: AsyncSession,
    client: TwilioNumbersClient,
    *,
    audit: Callable[[SyncResult], Awaitable[None]] | None = None,
) -> SyncResult:
    """Синхронизировать номера Twilio-аккаунта в ``phone_numbers`` как unassigned.

    Синхронный Twilio SDK выносится в threadpool (``asyncio.to_thread``), чтобы не
    блокировать event loop (ADR-0013). Upsert и (опциональный) аудит — в одной
    write-транзакции. ``TwilioNotConfiguredError`` / ``TwilioNumbersApiError``
    пробрасываются вызывающему (маппятся в 503/502).
    """
    if not client.is_configured:
        raise TwilioNotConfiguredError(
            "TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN не сконфигурированы"
        )

    raw = await asyncio.to_thread(client.list_incoming_numbers)
    synced_total = len(raw)
    normalized = _dedupe_normalize([(n.phone_number, n.friendly_name) for n in raw])

    repo = PhoneNumberRepository(session)
    async with session.begin():
        added = await repo.bulk_upsert_unassigned(normalized)
        result = SyncResult(
            synced_total=synced_total,
            added=added,
            skipped_existing=synced_total - added,
        )
        if audit is not None:
            await audit(result)

    _log.info(
        "twilio_numbers_synced",
        synced_total=result.synced_total,
        added=result.added,
        skipped_existing=result.skipped_existing,
    )
    return result
