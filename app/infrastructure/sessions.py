"""Серверные сессии в Redis (docs/08-security §2).

Два стора: :class:`SessionStore` (основные сессии, cookie ``sms_session``) и
:class:`SetupSessionStore` (setup-сессии ``/set-password``, cookie ``sms_setup``).

Ключи Redis: ``session:{token}`` → JSON, ``user_sessions:{user_id}`` → SET
токенов, ``setup_session:{token}`` → JSON.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Awaitable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, cast

import redis.asyncio as redis_asyncio

from shared.config import get_settings
from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)

SESSION_KEY_PREFIX = "session:"
USER_SESSIONS_KEY_PREFIX = "user_sessions:"
SETUP_SESSION_KEY_PREFIX = "setup_session:"


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _ua_hash(user_agent: str | None) -> str:
    if not user_agent:
        return ""
    return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:32]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class SessionData:
    """Payload сессии (Redis ``session:{token}``)."""

    user_id: int
    role: str  # super_admin | group_leader | group_member
    team_id: int | None
    csrf_token: str
    ip: str
    ua_hash: str
    created_at: str
    last_seen_at: str

    @classmethod
    def from_json(cls, raw: str) -> SessionData:
        d = json.loads(raw)
        d.setdefault("team_id", None)
        return cls(**d)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @property
    def is_super_admin(self) -> bool:
        return self.role == "super_admin"


@dataclass(slots=True)
class SetupSessionData:
    user_id: int
    csrf_token: str
    scope: str  # "set_password"
    created_at: str

    @classmethod
    def from_json(cls, raw: str) -> SetupSessionData:
        return cls(**json.loads(raw))

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


class SessionStore:
    """Основные сессии: скользящий TTL + абсолютный потолок."""

    def __init__(self, client: redis_asyncio.Redis | None = None) -> None:
        self._r = client or get_redis()
        s = get_settings()
        self._ttl = s.SESSION_TTL_SECONDS
        self._abs_ttl = s.SESSION_ABSOLUTE_TTL_SECONDS

    async def create(
        self,
        user_id: int,
        role: str,
        team_id: int | None,
        ip: str,
        ua: str | None,
    ) -> tuple[str, str]:
        if role not in {"super_admin", "group_leader", "group_member"}:
            raise ValueError(f"invalid role: {role!r}")
        if role == "super_admin" and team_id is not None:
            raise ValueError("super_admin must not have a team_id")
        if role != "super_admin" and team_id is None:
            raise ValueError(f"role={role!r} requires a non-null team_id")
        token = _new_token()
        csrf = _new_token()
        now = _now_iso()
        data = SessionData(
            user_id=user_id,
            role=role,
            team_id=team_id,
            csrf_token=csrf,
            ip=ip or "",
            ua_hash=_ua_hash(ua),
            created_at=now,
            last_seen_at=now,
        )
        async with self._r.pipeline(transaction=False) as pipe:
            pipe.set(SESSION_KEY_PREFIX + token, data.to_json(), ex=self._ttl)
            pipe.sadd(USER_SESSIONS_KEY_PREFIX + str(user_id), token)
            pipe.expire(USER_SESSIONS_KEY_PREFIX + str(user_id), self._abs_ttl)
            await pipe.execute()
        return token, csrf

    def _remaining_absolute(self, data: SessionData) -> int:
        """Секунды до абсолютного потолка жизни сессии (docs/08-security §2)."""
        try:
            created = datetime.fromisoformat(data.created_at)
        except ValueError:
            return 0
        age = (datetime.now(UTC) - created).total_seconds()
        return int(self._abs_ttl - age)

    async def get(self, token: str) -> SessionData | None:
        if not token:
            return None
        raw = await self._r.get(SESSION_KEY_PREFIX + token)
        if raw is None:
            return None
        try:
            data = SessionData.from_json(raw)
        except (json.JSONDecodeError, TypeError, KeyError):
            log.warning("session_corrupt_payload", token_prefix=token[:8])
            return None
        # Абсолютный потолок: сессия старше SESSION_ABSOLUTE_TTL_SECONDS —
        # инвалидируется независимо от скользящего TTL.
        if self._remaining_absolute(data) <= 0:
            await self.revoke(token)
            return None
        return data

    async def touch(self, token: str, data: SessionData) -> None:
        remaining_abs = self._remaining_absolute(data)
        if remaining_abs <= 0:
            await self.revoke(token)
            return
        data.last_seen_at = _now_iso()
        # Скользящий TTL, но не дольше остатка до абсолютного потолка.
        ex = max(1, min(self._ttl, remaining_abs))
        await self._r.set(SESSION_KEY_PREFIX + token, data.to_json(), ex=ex)

    async def revoke(self, token: str) -> None:
        if not token:
            return
        data = await self.get(token)
        async with self._r.pipeline(transaction=False) as pipe:
            pipe.delete(SESSION_KEY_PREFIX + token)
            if data is not None:
                pipe.srem(USER_SESSIONS_KEY_PREFIX + str(data.user_id), token)
            await pipe.execute()

    async def revoke_all_for_user(self, user_id: int) -> int:
        set_key = USER_SESSIONS_KEY_PREFIX + str(user_id)
        tokens = await cast("Awaitable[set[Any]]", self._r.smembers(set_key))
        if not tokens:
            return 0
        async with self._r.pipeline(transaction=False) as pipe:
            for t in tokens:
                pipe.delete(SESSION_KEY_PREFIX + t)
            pipe.delete(set_key)
            await pipe.execute()
        return len(tokens)


class SetupSessionStore:
    """Короткоживущая setup-сессия для первой установки пароля."""

    def __init__(self, client: redis_asyncio.Redis | None = None) -> None:
        self._r = client or get_redis()
        self._ttl = get_settings().SETUP_SESSION_TTL_SECONDS

    async def create(self, user_id: int) -> tuple[str, str]:
        token = _new_token()
        csrf = _new_token()
        data = SetupSessionData(
            user_id=user_id,
            csrf_token=csrf,
            scope="set_password",
            created_at=_now_iso(),
        )
        await self._r.set(
            SETUP_SESSION_KEY_PREFIX + token, data.to_json(), ex=self._ttl
        )
        return token, csrf

    async def get(self, token: str) -> SetupSessionData | None:
        if not token:
            return None
        raw = await self._r.get(SETUP_SESSION_KEY_PREFIX + token)
        if raw is None:
            return None
        try:
            return SetupSessionData.from_json(raw)
        except (json.JSONDecodeError, TypeError, KeyError):
            log.warning("setup_session_corrupt_payload", token_prefix=token[:8])
            return None

    async def revoke(self, token: str) -> None:
        if not token:
            return
        await self._r.delete(SETUP_SESSION_KEY_PREFIX + token)
