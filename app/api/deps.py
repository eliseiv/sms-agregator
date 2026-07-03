"""FastAPI-зависимости: сессия БД, текущая сессия/пользователь, guards.

Модель видимости (docs/05 §Guards): super_admin видит всё; остальные — свою
команду (``VisibilityScope.team_id``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ForbiddenError, NotAuthenticatedError
from app.infrastructure.repositories import UserRepository, UserTeamRepository
from app.infrastructure.sessions import SessionData, SessionStore
from shared.db import get_session
from shared.models import ROLE_GROUP_LEADER, ROLE_SUPER_ADMIN, User

Role = Literal["super_admin", "group_leader", "group_member"]


async def get_db() -> AsyncSession:  # type: ignore[misc]
    async for session in get_session():
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]


def current_session(request: Request) -> SessionData:
    sess: SessionData | None = getattr(request.state, "session", None)
    if sess is None:
        raise NotAuthenticatedError()
    return sess


CurrentSession = Annotated[SessionData, Depends(current_session)]


async def current_user(request: Request, db: DbSession, sess: CurrentSession) -> User:
    """Загрузить пользователя из сессии; при удалении — revoke + 401.

    Закрываем autobegun read-tx (``commit``), чтобы обработчики могли открыть
    свой ``async with db.begin():`` без «transaction already begun».
    """
    user = await UserRepository(db).get_by_id(sess.user_id)
    if user is None:
        token = getattr(request.state, "session_token", None)
        if token:
            await SessionStore().revoke(token)
        await db.rollback()
        raise NotAuthenticatedError(detail="Session user no longer exists")
    await db.commit()
    return user


CurrentUser = Annotated[User, Depends(current_user)]


@dataclass(frozen=True, slots=True)
class VisibilityScope:
    user_id: int
    role: Role
    team_id: int | None  # домашняя команда; None только для super_admin
    # ADR-0012: все команды участника (home ∪ доп. членства) из ``user_teams``.
    # Пусто для super_admin (видит всё вне scope-фильтра). Для не-super_admin
    # ``team_id`` всегда входит в этот набор.
    team_ids: frozenset[int]

    @property
    def is_super_admin(self) -> bool:
        return self.role == ROLE_SUPER_ADMIN

    @property
    def is_group_leader(self) -> bool:
        return self.role == ROLE_GROUP_LEADER


async def build_scope(user: User, db: AsyncSession) -> VisibilityScope:
    """Собрать :class:`VisibilityScope` из свежей строки БД (ADR-0012).

    Загружает полный набор команд из ``user_teams`` → scope несёт ``team_ids``
    (home ∪ доп.). ``super_admin`` членств не имеет (и видит всё независимо), его
    ``team_ids`` пуст. Home-членство всегда зеркалируется в ``user_teams`` (backfill
    + синхронизация в сервисах), но при возможном дрейфе домашняя команда всё равно
    добавляется (defense-in-depth).
    """
    if user.role == ROLE_SUPER_ADMIN:
        team_ids: frozenset[int] = frozenset()
    else:
        members = await UserTeamRepository(db).list_team_ids_for_user(user.id)
        team_ids = frozenset(members)
        if user.team_id is not None:
            team_ids |= {user.team_id}
    return VisibilityScope(
        user_id=user.id,
        role=user.role,  # type: ignore[arg-type]
        team_id=user.team_id,
        team_ids=team_ids,
    )


async def current_scope(user: CurrentUser, db: DbSession) -> VisibilityScope:
    """FastAPI-зависимость: построить scope запроса (ADR-0012).

    Построение non-admin scope читает ``user_teams`` — autobegun read-tx на общей
    сессии закрываем здесь (как в :func:`current_user`), чтобы обработчики могли
    открыть свой ``async with db.begin():`` без «transaction already begun».
    """
    scope = await build_scope(user, db)
    await db.commit()
    return scope


CurrentScope = Annotated[VisibilityScope, Depends(current_scope)]


def require_authenticated(user: CurrentUser) -> User:
    return user


def require_admin(user: CurrentUser) -> User:
    """super_admin only."""
    if user.role != ROLE_SUPER_ADMIN:
        raise ForbiddenError(detail="Только для администратора")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def require_admin_or_leader(user: CurrentUser) -> User:
    if user.role not in (ROLE_SUPER_ADMIN, ROLE_GROUP_LEADER):
        raise ForbiddenError(detail="Только для администратора или лидера")
    return user


def get_session_token(request: Request) -> str:
    token: str | None = getattr(request.state, "session_token", None)
    if not token:
        raise NotAuthenticatedError()
    return token
