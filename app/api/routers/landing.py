"""Корневой диспетчер `/` и SSR-landing участника/лидера `/app` (docs/05 §7, ADR-0008).

`GET /` — публичный маршрут-диспетчер: контент не рендерит, только 302 по роли
(нет сессии → `/login`; super_admin → `/admin`; участник/лидер → `/app`). Это
единая точка приземления пост-логин/пост-set-password (`303 → /`) и Mini App
SSO (`redirect:"/"`), поэтому 404 после логина исключён.

`GET /app` — SSR-страница участника/лидера: сервер инжектит в контекст номера
своей команды, статус собственной Telegram-привязки и `csrf_token`. Мутации идут
через существующие `/api/numbers` (нового API не вводится).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.api.deps import CurrentScope, CurrentSession, CurrentUser, DbSession
from app.api.templates import render
from app.infrastructure.repositories import (
    PhoneNumberRepository,
    TeamRepository,
    TelegramLinkRepository,
)
from app.infrastructure.sessions import SessionData

router = APIRouter(tags=["Landing"])


@router.get("/")
async def root_dispatch(request: Request) -> Response:
    """Диспетчер landing по роли. Публичный (без ``require_authenticated``),
    сам решает, куда редиректить; контент не рендерит.
    """
    sess: SessionData | None = getattr(request.state, "session", None)
    if sess is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    if sess.is_super_admin:
        return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
    return RedirectResponse(url="/app", status_code=status.HTTP_302_FOUND)


def _serialize_number(number: Any, team_name: str | None) -> dict[str, Any]:
    return {
        "id": number.id,
        "phone_number": number.phone_number,
        "team_id": number.team_id,
        "team_name": team_name,
        "label": number.label,
        "is_active": number.is_active,
        "added_by_user_id": number.added_by_user_id,
        "created_at": number.created_at.isoformat(),
    }


@router.get("/app", response_class=HTMLResponse)
async def app_landing(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    scope: CurrentScope,
    sess: CurrentSession,
) -> Response:
    """SSR-landing участника/лидера. super_admin (без команды) → 302 ``/admin``."""
    if scope.is_super_admin or scope.team_id is None:
        return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

    team_id = scope.team_id
    numbers_repo = PhoneNumberRepository(db)
    teams_repo = TeamRepository(db)
    links_repo = TelegramLinkRepository(db)

    numbers = await numbers_repo.list_by_team(team_id)
    team = await teams_repo.get(team_id)
    has_telegram_link = await links_repo.count_active_by_user_id(user.id) > 0

    team_name = team.name if team is not None else None
    context: dict[str, Any] = {
        "csrf_token": sess.csrf_token,
        "user_id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "team_id": team_id,
        "team_name": team_name,
        "has_telegram_link": has_telegram_link,
        "numbers": [_serialize_number(n, team_name) for n in numbers],
    }
    return await render(request, "app.html", context)
