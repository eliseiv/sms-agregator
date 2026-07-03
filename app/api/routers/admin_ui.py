"""Admin UI (SSR) — рендер Jinja2-страниц (docs/05-api-contracts §7).

Шаблоны создаёт frontend-агент; здесь — guard + сбор SSR-контекста + ``render``.
``/admin`` предгруппирует данные в Python (``user_groups`` по домашней команде +
``unassigned_numbers``), чтобы шаблон рендерил без доп. запросов (ADR-0015/0009).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.api.deps import CurrentSession, DbSession, require_admin
from app.api.templates import render
from app.application.admin_service import AdminService

router = APIRouter(tags=["Admin UI"], dependencies=[Depends(require_admin)])


@router.get("/admin", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    db: DbSession,
    sess: CurrentSession,
    q: str = Query(default="", max_length=64),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> Response:
    dashboard = await AdminService(db).users_page(q=q, page=page, limit=limit)
    context: dict[str, Any] = {
        **dashboard,
        "csrf_token": sess.csrf_token,
        "is_super_admin": True,
    }
    return await render(request, "admin/users.html", context)


@router.get("/admin/teams", response_class=HTMLResponse)
async def admin_teams_page(request: Request, sess: CurrentSession) -> Response:
    return await render(request, "admin/teams.html", {"csrf_token": sess.csrf_token})
