"""Redis fixed-window rate-limiting (docs/08-security §4).

INCR + EXPIRE(nx) — атомарный счётчик per окно. ``consume`` бросает
:class:`RateLimitedError` при превышении.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.exceptions import RateLimitedError
from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)


def client_ip(request: Request) -> str:
    """Best-effort client IP. Доверяет ``X-Forwarded-For`` от reverse-proxy."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "0.0.0.0"


@dataclass(frozen=True, slots=True)
class Limit:
    name: str
    capacity: int
    window_seconds: int


# docs/08-security §4 + Q-SEC-1 (10/min per IP + per username до пересмотра).
LIMIT_LOGIN_USERNAME = Limit(name="login_user", capacity=10, window_seconds=60)
LIMIT_LOGIN = Limit(name="login", capacity=10, window_seconds=60)
LIMIT_SET_PASSWORD = Limit(name="setpwd", capacity=10, window_seconds=60)
# Telegram Mini App SSO: 30/min per IP (до HMAC) + 10/min per tg_user_id (после).
LIMIT_TG_AUTH_IP = Limit(name="tg_auth_ip", capacity=30, window_seconds=60)
LIMIT_TG_AUTH_USER = Limit(name="tg_auth_user", capacity=10, window_seconds=60)
# Telegram webhook: 60/min per IP (до проверки секрет-токена), docs/08 §4.
LIMIT_TG_WEBHOOK_IP = Limit(name="tg_webhook_ip", capacity=60, window_seconds=60)


async def consume(limit: Limit, key: str) -> None:
    """Инкремент счётчика ``key`` и бросок при превышении capacity."""
    if not key:
        log.warning("rate_limit_no_key", limit_name=limit.name)
        return
    redis = get_redis()
    redis_key = f"rl:{limit.name}:{key}"
    async with redis.pipeline(transaction=False) as pipe:
        pipe.incr(redis_key)
        pipe.expire(redis_key, limit.window_seconds, nx=True)
        results = await pipe.execute()
    current = int(results[0])
    if current > limit.capacity:
        ttl = int(await redis.ttl(redis_key))
        raise RateLimitedError(
            detail="Слишком много попыток. Повторите позже.",
            retry_after=max(ttl, 1) if ttl > 0 else limit.window_seconds,
        )
