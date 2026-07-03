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
from app.api.serializers import serialize_number
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


@router.get("/app", response_class=HTMLResponse)
async def app_landing(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    scope: CurrentScope,
    sess: CurrentSession,
) -> Response:
    """SSR-landing участника/лидера. super_admin (без команды) → 302 ``/admin``.

    Multi-team (ADR-0012): scope = ``team_ids`` (все команды участника). Сервер
    инжектит номера ВСЕХ своих команд, сгруппированные по командам, + список
    команд ``teams`` для селектора в форме добавления.
    """
    if scope.is_super_admin or scope.team_id is None:
        return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

    home_team_id = scope.team_id
    numbers_repo = PhoneNumberRepository(db)
    teams_repo = TeamRepository(db)
    links_repo = TelegramLinkRepository(db)

    numbers = await numbers_repo.list_by_teams(scope.team_ids)
    all_teams = {t.id: t for t in await teams_repo.list_all()}
    has_telegram_link = await links_repo.count_active_by_user_id(user.id) > 0

    # Команды участника для селектора (home первой), отсортированы по имени.
    scope_team_ids = scope.team_ids
    teams = [
        {"id": tid, "name": all_teams[tid].name, "is_home": tid == home_team_id}
        for tid in sorted(
            scope_team_ids, key=lambda tid: (tid != home_team_id, all_teams[tid].name)
        )
        if tid in all_teams
    ]

    def _team_name(tid: int | None) -> str | None:
        team = all_teams.get(tid) if tid is not None else None
        return team.name if team is not None else None

    serialized = [serialize_number(n, _team_name(n.team_id)) for n in numbers]

    # Номера, сгруппированные по командам (порядок как в ``teams``).
    numbers_by_team = [
        {
            "team_id": t["id"],
            "team_name": t["name"],
            "is_home": t["is_home"],
            "numbers": [n for n in serialized if n["team_id"] == t["id"]],
        }
        for t in teams
    ]

    home_team_name = _team_name(home_team_id)
    context: dict[str, Any] = {
        "csrf_token": sess.csrf_token,
        "user_id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "team_id": home_team_id,
        "team_name": home_team_name,
        "teams": teams,
        "has_telegram_link": has_telegram_link,
        # Плоский список (все команды) — обратная совместимость с текущим шаблоном.
        "numbers": serialized,
        "numbers_by_team": numbers_by_team,
    }
    return await render(request, "app.html", context)
