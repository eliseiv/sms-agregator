"""AdminService — CRUD пользователей (super_admin-only). docs/05-api-contracts §4.

Все операции выполняет super_admin. Транзакцию открывает роутер (deferred
constraints). Инвариант роль↔team и правило «первый=лидер» соблюдаются здесь;
БД-триггеры — defense-in-depth.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import AuditWriter
from app.application.teams_service import TeamsService
from app.application.telegram_sso_service import TelegramSSOService
from app.exceptions import (
    ApiError,
    CannotAddSuperAdminToTeamError,
    CannotRemoveHomeMembershipError,
    ConflictError,
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
    NotFoundError,
)
from app.infrastructure.repositories import (
    PhoneNumberRepository,
    TeamRepository,
    TelegramLinkRepository,
    UserRepository,
    UserTeamRepository,
)
from app.infrastructure.sessions import SessionStore
from shared.logging import get_logger
from shared.models import ROLE_GROUP_MEMBER, ROLE_SUPER_ADMIN, User

log = get_logger(__name__)


def _validate_username(username: str) -> str:
    cleaned = (username or "").strip().lower()
    if not (3 <= len(cleaned) <= 64):
        raise ApiError(
            "invalid_username", "Логин должен быть 3..64 символа", status_code=400
        )
    if cleaned != cleaned.lower() or " " in cleaned:
        raise ApiError("invalid_username", "Недопустимый логин", status_code=400)
    return cleaned


class AdminService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._users = UserRepository(session)
        self._teams = TeamRepository(session)
        self._links = TelegramLinkRepository(session)
        self._memberships = UserTeamRepository(session)
        self._audit = AuditWriter(session)
        self._sessions = SessionStore()

    # --- List -------------------------------------------------------------

    def _user_item(
        self,
        user: User,
        *,
        team_name: str | None,
        is_leader: bool,
        active_link_user_ids: set[int],
        team_ids: list[int],
    ) -> dict[str, Any]:
        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "team_id": user.team_id,
            "team_name": team_name,
            "is_leader": is_leader,
            # ADR-0012: все команды пользователя (home + доп.) из user_teams.
            "team_ids": team_ids,
            "password_reset_required": user.password_reset_required,
            "has_telegram_link": user.id in active_link_user_ids,
            "created_at": user.created_at.isoformat(),
            "last_login_at": user.last_login_at.isoformat()
            if user.last_login_at
            else None,
        }

    async def list_users(self) -> list[dict[str, Any]]:
        """Плоский список для ``GET /api/admin/users`` в контрактном порядке (§4).

        Порядок: super_admin (по username) → команды (team_name ASC, team_id ASC)
        → внутри команды лидер первым (is_leader DESC), затем участники по username.
        """
        users = await self._users.list_all()
        teams = {t.id: t for t in await self._teams.list_all()}
        active_link_user_ids = await self._links.users_with_active_link(
            [u.id for u in users]
        )
        memberships = await self._memberships.list_team_ids_for_users(
            [u.id for u in users]
        )
        items: list[dict[str, Any]] = []
        for u in users:
            team = teams.get(u.team_id) if u.team_id is not None else None
            is_leader = team is not None and team.leader_user_id == u.id
            items.append(
                self._user_item(
                    u,
                    team_name=team.name if team is not None else None,
                    is_leader=is_leader,
                    active_link_user_ids=active_link_user_ids,
                    team_ids=memberships.get(u.id, []),
                )
            )

        def _sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
            is_super = item["role"] == ROLE_SUPER_ADMIN
            if is_super:
                return (0, "", 0, 0, item["username"])
            return (
                1,
                item["team_name"] or "",
                item["team_id"] or 0,
                0 if item["is_leader"] else 1,
                item["username"],
            )

        items.sort(key=_sort_key)
        return items

    # --- Grouped dashboard (SSR /admin, §7) --------------------------------

    async def grouped_dashboard(self) -> dict[str, Any]:
        """Предгруппированный контекст для SSR ``/admin`` (docs/05 §7, ADR-0012).

        Возвращает ``super_admins``, ``team_sections`` (по командам, лидер первым),
        ``teams`` (для select) и ``unassigned_numbers`` (пул, ADR-0009). Согласовано
        с ``list_users`` (те же поля + ``team_ids``), но представлено сгруппированно.

        Multi-team (ADR-0012): пользователь с несколькими членствами попадает в
        КАЖДУЮ свою ``team_section``. В домашней ``is_home=true`` (бейдж «домашняя»),
        в доп. — ``is_home=false``. Лидер помечается ``is_leader=true`` только в
        домашней команде. Группировка строится из bulk-членств ``user_teams``.
        """
        users = await self._users.list_all()
        teams_list = await self._teams.list_all()  # отсортированы по name
        teams = {t.id: t for t in teams_list}
        active_link_user_ids = await self._links.users_with_active_link(
            [u.id for u in users]
        )
        memberships = await self._memberships.list_team_ids_for_users(
            [u.id for u in users]
        )

        super_admins: list[dict[str, Any]] = []
        by_team: dict[int, list[dict[str, Any]]] = {}
        for u in users:
            home_team = teams.get(u.team_id) if u.team_id is not None else None
            is_home_leader = home_team is not None and home_team.leader_user_id == u.id
            user_team_ids = memberships.get(u.id, [])
            base = self._user_item(
                u,
                team_name=home_team.name if home_team is not None else None,
                is_leader=is_home_leader,
                active_link_user_ids=active_link_user_ids,
                team_ids=user_team_ids,
            )
            if u.role == ROLE_SUPER_ADMIN:
                super_admins.append(base)
                continue
            # Пользователь попадает в каждую свою команду (home + доп.).
            section_team_ids = set(user_team_ids)
            if u.team_id is not None:
                section_team_ids.add(u.team_id)
            for tid in section_team_ids:
                is_home = tid == u.team_id
                member_item = {
                    **base,
                    "is_home": is_home,
                    # Лидерство — только в домашней команде.
                    "is_leader": is_home and is_home_leader,
                }
                by_team.setdefault(tid, []).append(member_item)

        super_admins.sort(key=lambda i: i["username"])

        team_sections: list[dict[str, Any]] = []
        for t in sorted(teams_list, key=lambda x: (x.name, x.id)):
            members = by_team.get(t.id, [])
            members.sort(key=lambda i: (0 if i["is_leader"] else 1, i["username"]))
            team_sections.append(
                {
                    "team_id": t.id,
                    "team_name": t.name,
                    "leader_user_id": t.leader_user_id,
                    "members": members,
                }
            )

        numbers_repo = PhoneNumberRepository(self._db)
        unassigned = await numbers_repo.list_filtered(
            assignment="unassigned", team_id=None
        )
        unassigned_numbers = [
            {
                "id": n.id,
                "phone_number": n.phone_number,
                "label": n.label,
                "is_active": n.is_active,
                "created_at": n.created_at.isoformat(),
            }
            for n in unassigned
        ]

        return {
            "super_admins": super_admins,
            "team_sections": team_sections,
            "teams": [{"id": t.id, "name": t.name} for t in teams_list],
            "unassigned_numbers": unassigned_numbers,
        }

    # --- Create -----------------------------------------------------------

    async def create_user(
        self,
        *,
        actor_user_id: int,
        username: str,
        display_name: str | None,
        team_id: int | None,
        ip: str,
        user_agent: str | None,
    ) -> User:
        clean_username = _validate_username(username)
        # Инвариант роль↔team (docs/04): не-super_admin обязан иметь команду.
        if team_id is None:
            raise ApiError(
                "team_required",
                "Для создания пользователя нужна команда (team_id)",
                status_code=400,
            )
        team = await self._teams.get(team_id)
        if team is None:
            raise NotFoundError("team_not_found", "Команда не найдена")

        if await self._users.get_by_username(clean_username) is not None:
            raise ConflictError("username_taken", "Логин уже занят")

        try:
            user = await self._users.create(
                username=clean_username,
                role=ROLE_GROUP_MEMBER,
                team_id=team_id,
                display_name=display_name,
                password_hash=None,
                password_reset_required=True,
            )
        except IntegrityError as exc:
            raise ConflictError("username_taken", "Логин уже занят") from exc

        await self._audit.log(
            actor_user_id=actor_user_id,
            action="create_user",
            target_user_id=user.id,
            target_username=user.username,
            details={"team_id": team_id},
            ip=ip,
            user_agent=user_agent,
        )

        # ADR-0012: зеркалим домашнее членство в user_teams (та же транзакция).
        await self._memberships.add(user_id=user.id, team_id=team_id)

        # Правило «первый=лидер».
        role = await TeamsService(self._db).set_leader_if_absent(
            actor_user_id=actor_user_id,
            team_id=team_id,
            user_id=user.id,
            ip=ip,
            user_agent=user_agent,
        )
        if role != ROLE_GROUP_MEMBER:
            await self._users.update_fields(user.id, role=role)
            refreshed = await self._users.get_by_id(user.id)
            assert refreshed is not None
            return refreshed
        return user

    # --- Reset ------------------------------------------------------------

    async def reset_password(
        self, *, actor_user_id: int, target_id: int, ip: str, user_agent: str | None
    ) -> None:
        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError("user_not_found", "Пользователь не найден")
        if target.role == ROLE_SUPER_ADMIN:
            raise ApiError(
                "cannot_reset_super_admin",
                "Нельзя сбросить пароль super_admin",
                status_code=403,
            )
        await self._users.reset_password(target_id)
        await self._sessions.revoke_all_for_user(target_id)
        await self._audit.log(
            actor_user_id=actor_user_id,
            action="reset_password",
            target_user_id=target.id,
            target_username=target.username,
            ip=ip,
            user_agent=user_agent,
        )
        # docs §4: reset ревокает все telegram_links.
        await TelegramSSOService(self._db).revoke_for_user(
            user_id=target.id, reason="password_reset", ip=ip, user_agent=user_agent
        )

    # --- Delete -----------------------------------------------------------

    async def delete_user(
        self, *, actor_user_id: int, target_id: int, ip: str, user_agent: str | None
    ) -> None:
        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError("user_not_found", "Пользователь не найден")
        if target.role == ROLE_SUPER_ADMIN:
            raise ApiError(
                "cannot_delete_super_admin",
                "Нельзя удалить super_admin",
                status_code=403,
            )

        leader_nulled_team: int | None = None
        if target.role == "group_leader" and target.team_id is not None:
            # Лидерство/инвариант привязаны к users.team_id (home). Блокируют
            # удаление только ДРУГИЕ ДОМАШНИЕ участники (переназначаемые в лидеры),
            # а не доп.-участники другой home-команды (ADR-0012). Согласовано с
            # home-based disband-gate. target — home-участник → count > 1 ⇔ есть
            # другие домашние.
            if await self._users.count_home_members(target.team_id) > 1:
                raise ApiError(
                    "user_is_leader",
                    "Пользователь — лидер непустой команды; сначала переназначьте лидера",
                    status_code=409,
                )
            # Лидер и единственный ДОМАШНИЙ участник — обнуляем лидера, команда
            # становится пустой (доп.-участники остаются, лидерства не требуют).
            await self._teams.set_leader(team_id=target.team_id, leader_user_id=None)
            leader_nulled_team = target.team_id

        target_username = target.username
        await self._sessions.revoke_all_for_user(target_id)
        await self._users.delete(target_id)

        await self._audit.log(
            actor_user_id=actor_user_id,
            action="delete_user",
            target_user_id=target_id,
            target_username=target_username,
            ip=ip,
            user_agent=user_agent,
        )
        if leader_nulled_team is not None:
            await self._audit.log(
                actor_user_id=actor_user_id,
                action="team_leader_set",
                details={"team_id": leader_nulled_team, "leader_user_id": None},
                ip=ip,
                user_agent=user_agent,
            )

    # --- Update (PATCH) ---------------------------------------------------

    async def update_user(
        self,
        *,
        actor_user_id: int,
        target_id: int,
        set_display_name: bool,
        display_name: str | None,
        set_team: bool,
        team_id: int | None,
        ip: str,
        user_agent: str | None,
    ) -> User:
        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError("user_not_found", "Пользователь не найден")

        if set_team:
            await self._move_team(
                actor_user_id=actor_user_id,
                target=target,
                new_team_id=team_id,
                ip=ip,
                user_agent=user_agent,
            )

        if set_display_name:
            await self._users.update_fields(target_id, display_name=display_name)

        refreshed = await self._users.get_by_id(target_id)
        assert refreshed is not None
        return refreshed

    async def _move_team(
        self,
        *,
        actor_user_id: int,
        target: User,
        new_team_id: int | None,
        ip: str,
        user_agent: str | None,
    ) -> None:
        if target.role == ROLE_SUPER_ADMIN:
            raise ApiError(
                "role_team_invariant",
                "Нельзя переместить super_admin в команду",
                status_code=400,
            )
        if new_team_id is None:
            raise ApiError(
                "team_required",
                "Для перемещения нужна целевая команда (team_id)",
                status_code=400,
            )
        target_team = await self._teams.get(new_team_id)
        if target_team is None:
            raise NotFoundError("team_not_found", "Команда не найдена")

        old_team = target.team_id
        if old_team == new_team_id:
            return  # no-op

        leader_nulled_team: int | None = None
        if target.role == "group_leader" and old_team is not None:
            # Как и в delete_user: блокируют перенос только ДРУГИЕ ДОМАШНИЕ
            # участники команды-источника (users.team_id==old_team), а не
            # доп.-участники другой home-команды (ADR-0012). target — home →
            # count > 1 ⇔ есть другие домашние.
            if await self._users.count_home_members(old_team) > 1:
                raise ApiError(
                    "leader_move_forbidden",
                    "Лидер непустой команды не может быть перемещён; сначала переназначьте лидера",
                    status_code=409,
                )
            await self._teams.set_leader(team_id=old_team, leader_user_id=None)
            leader_nulled_team = old_team

        # Переносим (пока member — CHECK ok), затем правило «первый=лидер».
        await self._users.update_fields(
            target.id, team_id=new_team_id, role=ROLE_GROUP_MEMBER
        )
        # ADR-0012: синхронизируем домашнее членство — удаляем старую home-строку,
        # добавляем новую (add идемпотентен: дедуп, если новая home уже была доп.
        # членством). Доп. членства не трогаем.
        if old_team is not None:
            await self._memberships.remove(user_id=target.id, team_id=old_team)
        await self._memberships.add(user_id=target.id, team_id=new_team_id)

        role = await TeamsService(self._db).set_leader_if_absent(
            actor_user_id=actor_user_id,
            team_id=new_team_id,
            user_id=target.id,
            ip=ip,
            user_agent=user_agent,
        )
        if role != ROLE_GROUP_MEMBER:
            await self._users.update_fields(target.id, role=role)

        await self._sessions.revoke_all_for_user(target.id)
        await self._audit.log(
            actor_user_id=actor_user_id,
            action="user_team_change",
            target_user_id=target.id,
            target_username=target.username,
            details={"from_team_id": old_team, "to_team_id": new_team_id},
            ip=ip,
            user_agent=user_agent,
        )
        if leader_nulled_team is not None:
            await self._audit.log(
                actor_user_id=actor_user_id,
                action="team_leader_set",
                details={"team_id": leader_nulled_team, "leader_user_id": None},
                ip=ip,
                user_agent=user_agent,
            )

    # --- Доп. членство (ADR-0012) -----------------------------------------

    async def add_membership(
        self,
        *,
        actor_user_id: int,
        target_id: int,
        team_id: int,
        ip: str,
        user_agent: str | None,
    ) -> dict[str, Any]:
        """Добавить дополнительное членство (docs/05 §4, ADR-0012).

        super_admin-only (guard роутера). Target не может быть super_admin.
        Идемпотентно через UNIQUE — повтор → ``409 membership_already_exists``.
        Не меняет ``users.team_id`` (домашнюю) и ``users.role``. Ревокует сессии
        target, чтобы ``VisibilityScope.team_ids`` перечитался из ``user_teams``.
        """
        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError("user_not_found", "Пользователь не найден")
        if target.role == ROLE_SUPER_ADMIN:
            raise CannotAddSuperAdminToTeamError(
                detail="Нельзя добавить super_admin в команду"
            )
        if await self._teams.get(team_id) is None:
            raise NotFoundError("team_not_found", "Команда не найдена")

        created = await self._memberships.add(user_id=target_id, team_id=team_id)
        if not created:
            raise MembershipAlreadyExistsError(
                detail="Пользователь уже состоит в этой команде"
            )

        await self._sessions.revoke_all_for_user(target_id)
        await self._audit.log(
            actor_user_id=actor_user_id,
            action="user_team_add",
            target_user_id=target_id,
            target_username=target.username,
            details={"team_id": team_id},
            ip=ip,
            user_agent=user_agent,
        )
        return {"user_id": target_id, "team_id": team_id}

    async def remove_membership(
        self,
        *,
        actor_user_id: int,
        target_id: int,
        team_id: int,
        ip: str,
        user_agent: str | None,
    ) -> None:
        """Убрать дополнительное членство (docs/05 §4, ADR-0012).

        super_admin-only. Домашнее членство (``team_id == users.team_id``) убрать
        нельзя — для этого есть move (``PATCH``). Несуществующее доп. членство →
        ``404 membership_not_found``. Ревокует сессии target при успехе.
        """
        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError("user_not_found", "Пользователь не найден")
        if target.team_id is not None and target.team_id == team_id:
            raise CannotRemoveHomeMembershipError(
                detail="Нельзя убрать домашнюю команду; смена — через перемещение"
            )

        removed = await self._memberships.remove(user_id=target_id, team_id=team_id)
        if not removed:
            raise MembershipNotFoundError(detail="Такого членства нет")

        await self._sessions.revoke_all_for_user(target_id)
        await self._audit.log(
            actor_user_id=actor_user_id,
            action="user_team_remove",
            target_user_id=target_id,
            target_username=target.username,
            details={"team_id": team_id},
            ip=ip,
            user_agent=user_agent,
        )
