"""Admin UI (SSR) — рендер Jinja2-страниц (docs/05-api-contracts §7).

Шаблоны создаёт frontend-агент; здесь — только guard + вызовы ``render``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.api.deps import require_admin
from app.api.templates import render

router = APIRouter(tags=["Admin UI"], dependencies=[Depends(require_admin)])


@router.get("/admin", response_class=HTMLResponse)
async def admin_users_page(request: Request) -> Response:
    return await render(request, "admin/users.html", {})


@router.get("/admin/teams", response_class=HTMLResponse)
async def admin_teams_page(request: Request) -> Response:
    return await render(request, "admin/teams.html", {})
