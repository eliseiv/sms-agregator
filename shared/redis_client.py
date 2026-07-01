"""Единый async Redis-клиент (сессии, pending-токены, rate-limit).

Файл называется ``redis_client.py`` (не ``redis.py``), чтобы не затенять
пакет ``redis`` при импорте.
"""

from __future__ import annotations

import redis.asyncio as redis_asyncio

from shared.config import get_settings

_redis: redis_asyncio.Redis | None = None


def get_redis() -> redis_asyncio.Redis:
    """Process-wide async Redis-клиент."""
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = redis_asyncio.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
        )
    return _redis


async def close_redis() -> None:
    """Закрыть глобальный клиент. Shutdown-хук и тесты."""
    global _redis
    if _redis is not None:
        close_method = getattr(_redis, "aclose", None) or _redis.close
        await close_method()
        _redis = None
