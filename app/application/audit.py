"""AuditWriter — обёртка над AuditRepository с закрытым enum действий.

Ошибка записи аудита пробрасывается, чтобы вызывающая бизнес-операция могла
откатиться (не глушим).
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.repositories import AuditRepository

# Закрытый enum из docs/04-data-model.md таблица ``admin_audit``.
ALLOWED_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "admin_login",
        "admin_logout",
        "create_user",
        "reset_password",
        "delete_user",
        "lockout_triggered",
        "team_create",
        "team_rename",
        "team_delete",
        "team_leader_set",
        "user_team_change",
        "user_team_add",
        "user_team_remove",
        "number_added",
        "number_removed",
        "number_team_assigned",
        "telegram_link_created",
        "telegram_link_revoked",
        "telegram_link_dead_marked",
        "telegram_link_rebound",
    }
)


class AuditWriter:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = AuditRepository(session)

    async def log(
        self,
        *,
        actor_user_id: int,
        action: str,
        target_user_id: int | None = None,
        target_username: str | None = None,
        details: dict[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"Audit action not in enum: {action!r}")
        await self._repo.insert(
            actor_user_id=actor_user_id,
            action=action,
            target_user_id=target_user_id,
            target_username=target_username,
            details=details,
            ip=ip,
            user_agent=user_agent,
        )
