"""Фоновые задачи: цикл переотправки доставок (docs/03-architecture)."""

from __future__ import annotations

import asyncio

from app.application.services import retry_pending_deliveries
from app.infrastructure.telegram_api import get_telegram_client
from shared.config import Settings
from shared.db import make_session
from shared.logging import get_logger

log = get_logger(__name__)


async def delivery_retry_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    """Периодически переотправляет pending/failed доставки."""
    telegram = get_telegram_client(settings)
    while not stop_event.is_set():
        try:
            async with make_session() as session:
                await retry_pending_deliveries(session, telegram, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("delivery_retry_loop_error", exc_info=True)
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.DELIVERY_RETRY_INTERVAL_SECONDS
            )
        except TimeoutError:
            continue
