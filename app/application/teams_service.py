"""TeamsService — CRUD команд, назначение лидера, правило «первый=лидер».

Порядок операций и коды ошибок — docs/05-api-contracts §5. Все мутации
super_admin-only (кроме set_leader_if_absent, вызываемого из admin-flow).
Вызывается внутри транзакции, открытой роутером (deferred constraints).
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import AuditWriter
from app.exceptions import ApiError, NotFoundError, ValidationError
from app.infrastructure.repositories import TeamRepository, UserRepository
from app.infrastructure.sessions import SessionStore
from shared.logging import get_logger
from shared.models import ROLE_GROUP_LEADER, ROLE_GROUP_MEMBER, Team

log = get_logger(__name__)


def _validate_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not (1 <= len(cleaned) <= 100):
        raise ApiError(
            "invalid_name", "Имя команды должно быть 1..100 символов", status_code=400
        )
    return cleaned


class TeamsService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._teams = TeamRepository(session)
        self._users = UserRepository(session)
        self._audit = AuditWriter(session)
        self._sessions = SessionStore()

    # --- CRUD -------------------------------------------------------------

    async def create(
        self, *, actor_user_id: int, name: str, ip: str, user_agent: str | None
    ) -> Team:
        cleaned = _validate_name(name)
        if await self._teams.get_by_name(cleaned) is not None:
            raise ApiError(
                "team_name_taken", "Команда с таким именем уже есть", status_code=409
            )
        try:
            team = await self._teams.create(name=cleaned, leader_user_id=None)
        except IntegrityError as exc:
            raise ApiError(
                "team_name_taken", "Команда с таким именем уже есть", status_code=409
            ) from exc
        await self._audit.log(
            actor_user_id=actor_user_id,
            action="team_create",
            details={"team_id": team.id, "name": cleaned},
            ip=ip,
            user_agent=user_agent,
        )
        return team

    async def rename(
        self,
        *,
        actor_user_id: int,
        team_id: int,
        name: str,
        ip: str,
        user_agent: str | None,
    ) -> Team:
        cleaned = _validate_name(name)
        team = await self._teams.get(team_id)
        if team is None:
            raise NotFoundError("team_not_found", "Команда не найдена")
        other = await self._teams.get_by_name(cleaned)
        if other is not None and other.id != team_id:
            raise ApiError(
                "team_name_taken", "Команда с таким именем уже есть", status_code=409
            )
        from_name = team.name
        try:
            await self._teams.rename(team_id=team_id, name=cleaned)
        except IntegrityError as exc:
            raise ApiError(
                "team_name_taken", "Команда с таким именем уже есть", status_code=409
            ) from exc
        await self._audit.log(
            actor_user_id=actor_user_id,
            action="team_rename",
            details={"team_id": team_id, "from_name": from_name, "to_name": cleaned},
            ip=ip,
            user_agent=user_agent,
        )
        refreshed = await self._teams.get(team_id)
        assert refreshed is not None
        return refreshed

    async def delete(
        self, *, actor_user_id: int, team_id: int, ip: str, user_agent: str | None
    ) -> None:
        team = await self._teams.get(team_id)
        if team is None:
            raise NotFoundError("team_not_found", "Команда не найдена")
        members = await self._users.list_user_ids_in_team(team_id)
        if team.leader_user_id is not None or members:
            raise ApiError(
                "team_has_members",
                "Команда не пуста — сначала распустите участников",
                status_code=409,
            )
        await self._teams.delete(team_id)
        await self._audit.log(
            actor_user_id=actor_user_id,
            action="team_delete",
            details={"team_id": team_id, "name": team.name},
            ip=ip,
            user_agent=user_agent,
        )

    # --- Смена лидера внутри команды --------------------------------------

    async def set_leader(
        self,
        *,
        actor_user_id: int,
        team_id: int,
        new_leader_user_id: int,
        ip: str,
        user_agent: str | None,
    ) -> Team:
        team = await self._teams.get(team_id)
        if team is None:
            raise NotFoundError("team_not_found", "Команда не найдена")
        new_leader = await self._users.get_by_id(new_leader_user_id)
        if new_leader is None:
            raise NotFoundError("user_not_found", "Пользователь не найден")
        if new_leader.team_id != team_id:
            raise ApiError(
                "user_not_in_team",
                "Кандидат не является участником этой команды",
                status_code=400,
            )

        previous_leader_user_id = team.leader_user_id
        # Порядок: снять текущего лидера (→ member), назначить нового, обновить team.
        if (
            previous_leader_user_id is not None
            and previous_leader_user_id != new_leader_user_id
        ):
            await self._users.update_fields(
                previous_leader_user_id, role=ROLE_GROUP_MEMBER
            )
        await self._users.update_fields(new_leader_user_id, role=ROLE_GROUP_LEADER)
        await self._teams.set_leader(team_id=team_id, leader_user_id=new_leader_user_id)

        # Инвалидируем сессии затронутых пользователей (роль поменялась).
        await self._sessions.revoke_all_for_user(new_leader_user_id)
        if (
            previous_leader_user_id is not None
            and previous_leader_user_id != new_leader_user_id
        ):
            await self._sessions.revoke_all_for_user(previous_leader_user_id)

        await self._audit.log(
            actor_user_id=actor_user_id,
            action="team_leader_set",
            target_user_id=new_leader_user_id,
            details={
                "team_id": team_id,
                "previous_leader_user_id": previous_leader_user_id,
                "new_leader_user_id": new_leader_user_id,
            },
            ip=ip,
            user_agent=user_agent,
        )
        refreshed = await self._teams.get(team_id)
        assert refreshed is not None
        return refreshed

    # --- Правило «первый=лидер» -------------------------------------------

    async def set_leader_if_absent(
        self,
        *,
        actor_user_id: int,
        team_id: int,
        user_id: int,
        ip: str,
        user_agent: str | None,
    ) -> str:
        """При добавлении первого участника в orphan-команду — он становится лидером.

        Возвращает роль, которую должен получить пользователь в целевой команде
        (``group_leader`` если команда была пустой, иначе ``group_member``).
        Обновляет ``teams.leader_user_id`` при назначении лидера.
        """
        team = await self._teams.get(team_id)
        if team is None:
            raise ValidationError("team_not_found", "Целевая команда не найдена")
        if team.leader_user_id is None:
            await self._teams.set_leader(team_id=team_id, leader_user_id=user_id)
            await self._audit.log(
                actor_user_id=actor_user_id,
                action="team_leader_set",
                target_user_id=user_id,
                details={
                    "team_id": team_id,
                    "new_leader_user_id": user_id,
                    "auto_first": True,
                },
                ip=ip,
                user_agent=user_agent,
            )
            return ROLE_GROUP_LEADER
        return ROLE_GROUP_MEMBER
