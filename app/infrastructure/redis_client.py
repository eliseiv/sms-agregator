"""Re-export единого Redis-клиента из shared (docs/03-architecture)."""

from __future__ import annotations

from shared.redis_client import close_redis, get_redis

__all__ = ["close_redis", "get_redis"]
