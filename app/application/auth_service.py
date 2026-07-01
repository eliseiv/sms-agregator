"""AuthService — login, set-password, logout, seed super_admin (docs/03, docs/08).

Анти-timing: при неизвестном логине всё равно выполняем argon2 verify против
фиксированного dummy-hash — одинаковое время ответа.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import AuditWriter
from app.core.security import (
    DUMMY_HASH,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.exceptions import AccountLockedError, NotAuthenticatedError
from app.infrastructure.repositories import UserRepository
from app.infrastructure.sessions import SessionStore, SetupSessionStore
from shared.config import get_settings
from shared.logging import get_logger
from shared.models import ROLE_SUPER_ADMIN

log = get_logger(__name__)


@dataclass(slots=True)
class LoginResult:
    kind: Literal["session_created", "set_password_required", "invalid", "locked"]
    session_token: str | None = None
    setup_token: str | None = None
    csrf: str | None = None
    role: str | None = None
    team_id: int | None = None
    user_id: int | None = None
    retry_after_sec: int | None = None
    is_admin: bool = False


@dataclass(slots=True)
class LoginLookupResult:
    kind: Literal["not_found", "set_password_required", "ready_for_password"]
    user_id: int | None = None
    setup_token: str | None = None


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._users = UserRepository(session)
        self._audit = AuditWriter(session)
        self._sessions = SessionStore()
        self._setup = SetupSessionStore()
        self._settings = get_settings()

    # --- Шаг-1: только логин (анти-энумерация) ----------------------------

    async def lookup_for_login(self, *, username: str) -> LoginLookupResult:
        user = await self._users.get_by_username(username)
        if user is None:
            return LoginLookupResult(kind="not_found")
        if user.password_reset_required or user.password_hash is None:
            setup_token, _csrf = await self._setup.create(user.id)
            return LoginLookupResult(
                kind="set_password_required", user_id=user.id, setup_token=setup_token
            )
        return LoginLookupResult(kind="ready_for_password", user_id=user.id)

    # --- Шаг-2: пароль ----------------------------------------------------

    async def login(
        self, *, username: str, password: str, ip: str, user_agent: str | None
    ) -> LoginResult:
        user = await self._users.get_by_username(username)

        # Lockout — до проверки пароля.
        if user is not None and user.lockout_until is not None:
            now = datetime.now(UTC)
            if user.lockout_until > now:
                retry = int((user.lockout_until - now).total_seconds())
                return LoginResult(kind="locked", retry_after_sec=max(retry, 1))

        # Требуется установка пароля.
        if user is not None and user.password_reset_required:
            # Anti-timing: выполняем dummy-verify, чтобы время этой ветки
            # совпадало с обычным verify-путём (анти-энумерация, TD-010).
            verify_password(DUMMY_HASH, password)
            setup_token, csrf = await self._setup.create(user.id)
            return LoginResult(
                kind="set_password_required",
                setup_token=setup_token,
                csrf=csrf,
                user_id=user.id,
            )

        # Обычный verify — всегда вызываем verify для timing-parity.
        password_hash = user.password_hash if user is not None else DUMMY_HASH
        if password_hash is None:
            if user is not None:
                # Anti-timing (см. выше): dummy-verify перед setup-токеном.
                verify_password(DUMMY_HASH, password)
                setup_token, csrf = await self._setup.create(user.id)
                return LoginResult(
                    kind="set_password_required",
                    setup_token=setup_token,
                    csrf=csrf,
                    user_id=user.id,
                )
            password_hash = DUMMY_HASH

        verified = verify_password(password_hash, password)

        if not verified or user is None:
            if user is not None:
                attempts, lockout = await self._users.record_login_failure(
                    user.id,
                    threshold=self._settings.LOGIN_FAILURE_THRESHOLD,
                    lockout_minutes=self._settings.LOGIN_LOCKOUT_MINUTES,
                )
                if (
                    lockout is not None
                    and attempts == self._settings.LOGIN_FAILURE_THRESHOLD
                ):
                    await self._audit.log(
                        actor_user_id=user.id,
                        action="lockout_triggered",
                        target_user_id=user.id,
                        target_username=user.username,
                        ip=ip,
                        user_agent=user_agent,
                    )
            return LoginResult(kind="invalid")

        # Перехеширование при смене параметров argon2.
        if needs_rehash(password_hash):
            await self._users.set_password_hash(user.id, hash_password(password))

        await self._users.record_login_success(user.id)

        is_super = user.role == ROLE_SUPER_ADMIN
        token, csrf = await self._sessions.create(
            user.id, user.role, user.team_id, ip, user_agent
        )

        if is_super:
            await self._audit.log(
                actor_user_id=user.id,
                action="admin_login",
                ip=ip,
                user_agent=user_agent,
            )

        return LoginResult(
            kind="session_created",
            session_token=token,
            csrf=csrf,
            role=user.role,
            team_id=user.team_id,
            user_id=user.id,
            is_admin=is_super,
        )

    # --- Установка пароля -------------------------------------------------

    async def complete_set_password(
        self, *, setup_token: str, password: str, ip: str, user_agent: str | None
    ) -> LoginResult:
        setup = await self._setup.get(setup_token)
        if setup is None or setup.scope != "set_password":
            raise NotAuthenticatedError(detail="Setup session expired")

        user = await self._users.get_by_id(setup.user_id)
        if user is None:
            raise NotAuthenticatedError(detail="User no longer exists")

        await self._users.set_password_hash(user.id, hash_password(password))
        await self._setup.revoke(setup_token)
        await self._users.record_login_success(user.id)

        is_super = user.role == ROLE_SUPER_ADMIN
        session_token, csrf = await self._sessions.create(
            user.id, user.role, user.team_id, ip, user_agent
        )

        if is_super:
            await self._audit.log(
                actor_user_id=user.id,
                action="admin_login",
                ip=ip,
                user_agent=user_agent,
            )

        return LoginResult(
            kind="session_created",
            session_token=session_token,
            csrf=csrf,
            role=user.role,
            team_id=user.team_id,
            user_id=user.id,
            is_admin=is_super,
        )

    # --- Logout -----------------------------------------------------------

    async def logout(
        self,
        *,
        session_token: str,
        actor_user_id: int,
        is_admin: bool,
        ip: str,
        user_agent: str | None,
    ) -> None:
        if is_admin:
            await self._audit.log(
                actor_user_id=actor_user_id,
                action="admin_logout",
                ip=ip,
                user_agent=user_agent,
            )
        await self._sessions.revoke(session_token)


def raise_locked_if_needed(retry_sec: int | None) -> None:
    if retry_sec is not None:
        raise AccountLockedError(
            detail="Аккаунт временно заблокирован из-за неудачных попыток.",
            retry_after=retry_sec,
        )


# --- Seed super_admin -------------------------------------------------------


async def seed_admin(session: AsyncSession) -> str:
    """Идемпотентный upsert super_admin из env (docs/08-security §1).

    Гарантирует ровно одного super_admin: ищет существующего (partial-UNIQUE
    ``users_single_super_admin``); если найден и username != ADMIN_LOGIN —
    переименовывает; иначе обновляет пароль; если не найден — INSERT.
    Возвращает ``created`` / ``updated`` / ``unchanged``.
    """
    settings = get_settings()
    repo = UserRepository(session)
    login = settings.ADMIN_LOGIN.strip().lower()

    existing = await repo.get_super_admin()

    if existing is not None:
        same_login = existing.username == login
        same_password = bool(existing.password_hash) and verify_password(
            existing.password_hash or "", settings.ADMIN_PASSWORD
        )
        if (
            same_login
            and same_password
            and not existing.password_reset_required
            and existing.lockout_until is None
            and existing.failed_login_attempts == 0
        ):
            log.info("admin_seed_unchanged", username=login)
            return "unchanged"
        await repo.update_fields(
            existing.id,
            username=login,
            password_hash=hash_password(settings.ADMIN_PASSWORD),
            role=ROLE_SUPER_ADMIN,
            team_id=None,
            password_reset_required=False,
            failed_login_attempts=0,
            lockout_until=None,
        )
        log.info("admin_seed_updated", username=login)
        return "updated"

    await repo.create(
        username=login,
        role=ROLE_SUPER_ADMIN,
        team_id=None,
        password_hash=hash_password(settings.ADMIN_PASSWORD),
        password_reset_required=False,
    )
    log.info("admin_seed_created", username=login)
    return "created"
