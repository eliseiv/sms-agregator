"""Telegram Persistent SSO сервис (ADR-0004).

- ``verify_and_resolve`` — HMAC-валидация initData + резолв ``telegram_links``.
- ``create_pending`` / ``consume_pending`` — one-shot Redis-токен (cookie sms_tg_pending).
- ``link_pending`` — привязка после успешного логина (rebind разрешён).
- ``self_heal_link`` — идемпотентное восстановление привязки для уже-залогиненного.
- ``mark_link_dead`` — пометка привязки мёртвой (403 от Bot API).
- ``revoke_for_user`` / ``revoke_one`` — отзыв привязок.

Мягкий потолок ``TG_MAX_LINKS_PER_USER``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import AuditWriter
from app.exceptions import TelegramLinkLimitError, TelegramLinkOwnedByOtherError
from app.infrastructure.repositories import TelegramLinkRepository
from app.telegram.init_data import (
    InitDataError,
    ValidatedInitData,
    verify_init_data,
)
from shared.config import get_settings
from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)

TG_PENDING_KEY_PREFIX: Final[str] = "tg_pending:"


def _new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class SSOResolved:
    kind: Literal["linked", "unlinked"]
    telegram_user_id: int
    user_id: int | None
    validated: ValidatedInitData


class InvalidInitDataError(Exception):
    def __init__(self, reason: InitDataError) -> None:
        super().__init__(reason)
        self.reason = reason


class TelegramSSOService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._links = TelegramLinkRepository(session)
        self._audit = AuditWriter(session)
        self._settings = get_settings()

    # --- validation + lookup ----------------------------------------------

    async def verify_and_resolve(self, init_data: str) -> SSOResolved:
        outcome = verify_init_data(
            init_data,
            bot_token=self._settings.TELEGRAM_BOT_TOKEN,
            max_age_seconds=self._settings.TG_AUTH_INIT_DATA_TTL_SECONDS,
        )
        if not isinstance(outcome, ValidatedInitData):
            raise InvalidInitDataError(outcome)

        link = await self._links.get_active_by_telegram_user_id(
            outcome.telegram_user_id
        )
        if link is not None:
            return SSOResolved(
                kind="linked",
                telegram_user_id=outcome.telegram_user_id,
                user_id=link.user_id,
                validated=outcome,
            )
        return SSOResolved(
            kind="unlinked",
            telegram_user_id=outcome.telegram_user_id,
            user_id=None,
            validated=outcome,
        )

    # --- pending token ----------------------------------------------------

    async def create_pending(self, telegram_user_id: int) -> str:
        token = _new_token()
        redis = get_redis()
        await redis.set(
            TG_PENDING_KEY_PREFIX + token,
            str(telegram_user_id),
            ex=self._settings.TG_PENDING_LINK_TTL_SECONDS,
        )
        return token

    async def consume_pending(self, token: str) -> int | None:
        if not token:
            return None
        redis = get_redis()
        key = TG_PENDING_KEY_PREFIX + token
        async with redis.pipeline(transaction=False) as pipe:
            pipe.get(key)
            pipe.delete(key)
            results = await pipe.execute()
        raw = results[0]
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            log.warning("tg_pending_corrupt_value", token_prefix=token[:8])
            return None

    # --- linking ----------------------------------------------------------

    async def link_pending(
        self, *, telegram_user_id: int, user_id: int, ip: str, user_agent: str | None
    ) -> None:
        await self._link(
            telegram_user_id=telegram_user_id,
            user_id=user_id,
            ip=ip,
            user_agent=user_agent,
            allow_rebind_from_other=True,
            via="login_flow",
        )

    async def link_session_add(
        self, *, telegram_user_id: int, user_id: int, ip: str, user_agent: str | None
    ) -> None:
        await self._link(
            telegram_user_id=telegram_user_id,
            user_id=user_id,
            ip=ip,
            user_agent=user_agent,
            allow_rebind_from_other=False,
            via="session_add",
        )

    async def self_heal_link(
        self, *, telegram_user_id: int, user_id: int, ip: str, user_agent: str | None
    ) -> bool:
        """Идемпотентно восстановить привязку для уже-залогиненного (best-effort).

        Владеет своей транзакцией (``db.begin()``). Никогда не пробрасывает
        исключение — вернёт ``False`` при внутренней ошибке.
        """
        try:
            async with self._db.begin():
                await self._link(
                    telegram_user_id=telegram_user_id,
                    user_id=user_id,
                    ip=ip,
                    user_agent=user_agent,
                    allow_rebind_from_other=True,
                    via="self_heal",
                )
        except Exception:
            log.warning(
                "telegram_self_heal_failed",
                user_id=user_id,
                telegram_user_id=telegram_user_id,
            )
            return False
        return True

    async def _link(
        self,
        *,
        telegram_user_id: int,
        user_id: int,
        ip: str,
        user_agent: str | None,
        allow_rebind_from_other: bool,
        via: str,
    ) -> None:
        existing = await self._links.get_by_telegram_user_id(telegram_user_id)

        # Привязка принадлежит другому пользователю.
        if existing is not None and existing.user_id != user_id:
            if not allow_rebind_from_other:
                raise TelegramLinkOwnedByOtherError(
                    detail="Этот Telegram-аккаунт привязан к другому пользователю"
                )
            await self._links.upsert(telegram_user_id=telegram_user_id, user_id=user_id)
            await self._audit.log(
                actor_user_id=user_id,
                action="telegram_link_rebound",
                target_user_id=user_id,
                details={
                    "telegram_user_id": telegram_user_id,
                    "previous_user_id": existing.user_id,
                    "via": via,
                },
                ip=ip,
                user_agent=user_agent,
            )
            return

        # Привязка уже у этого пользователя.
        if existing is not None and existing.user_id == user_id:
            if existing.dead_at is None:
                # Живая привязка — полный NO-OP (не двигаем created_at, без аудита).
                return
            await self._links.upsert(telegram_user_id=telegram_user_id, user_id=user_id)
            await self._audit.log(
                actor_user_id=user_id,
                action="telegram_link_created",
                target_user_id=user_id,
                details={
                    "telegram_user_id": telegram_user_id,
                    "replaced": True,
                    "via": via,
                },
                ip=ip,
                user_agent=user_agent,
            )
            return

        # Новая привязка — мягкий потолок.
        active = await self._links.count_active_by_user_id(user_id)
        if active >= self._settings.TG_MAX_LINKS_PER_USER:
            log.info(
                "telegram_link_limit_reached",
                user_id=user_id,
                active_links=active,
                limit=self._settings.TG_MAX_LINKS_PER_USER,
            )
            if not allow_rebind_from_other:
                raise TelegramLinkLimitError(detail="Достигнут лимит привязок Telegram")
            return

        await self._links.upsert(telegram_user_id=telegram_user_id, user_id=user_id)
        await self._audit.log(
            actor_user_id=user_id,
            action="telegram_link_created",
            target_user_id=user_id,
            details={
                "telegram_user_id": telegram_user_id,
                "replaced": False,
                "via": via,
            },
            ip=ip,
            user_agent=user_agent,
        )

    # --- revoke -----------------------------------------------------------

    async def revoke_for_user(
        self, *, user_id: int, reason: str, ip: str, user_agent: str | None
    ) -> None:
        deleted = await self._links.delete_all_by_user_id(user_id)
        if not deleted:
            return
        await self._audit.log(
            actor_user_id=user_id,
            action="telegram_link_revoked",
            target_user_id=user_id,
            details={"telegram_user_ids": deleted, "reason": reason},
            ip=ip,
            user_agent=user_agent,
        )

    async def revoke_one(
        self, *, user_id: int, telegram_user_id: int, ip: str, user_agent: str | None
    ) -> bool:
        deleted = await self._links.delete_one(
            user_id=user_id, telegram_user_id=telegram_user_id
        )
        if not deleted:
            return False
        await self._audit.log(
            actor_user_id=user_id,
            action="telegram_link_revoked",
            target_user_id=user_id,
            details={"telegram_user_id": telegram_user_id, "reason": "user_unlink"},
            ip=ip,
            user_agent=user_agent,
        )
        return True

    # --- dead-link marker -------------------------------------------------

    async def mark_link_dead(
        self, *, telegram_user_id: int, user_id: int, reason: str
    ) -> None:
        await self._links.mark_dead(telegram_user_id)
        await self._audit.log(
            actor_user_id=user_id,
            action="telegram_link_dead_marked",
            target_user_id=user_id,
            details={"telegram_user_id": telegram_user_id, "reason": reason},
        )
