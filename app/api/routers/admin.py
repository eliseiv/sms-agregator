"""Admin JSON API: пользователи и команды (docs/05-api-contracts §4, §5).

Все endpoints — ``require_admin`` (super_admin). Транзакции открываются здесь;
доменные ошибки конвертируются в плоский envelope глобальным обработчиком.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.api.deps import DbSession, require_admin
from app.api.schemas import (
    AddMembershipRequest,
    CreateTeamRequest,
    CreateUserRequest,
    RenameTeamRequest,
    SetLeaderRequest,
    UpdateUserRequest,
)
from app.application.admin_service import AdminService
from app.application.teams_service import TeamsService
from app.exceptions import ValidationError
from app.infrastructure.rate_limit import client_ip
from app.infrastructure.repositories import TeamRepository, UserRepository

router = APIRouter(
    prefix="/api/admin", tags=["Admin"], dependencies=[Depends(require_admin)]
)


async def _read_body(request: Request) -> dict[str, Any]:
    ct = request.headers.get("content-type", "")
    if ct.startswith("application/json"):
        try:
            data = await request.json()
        except ValueError as exc:
            raise ValidationError(detail="Body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise ValidationError(detail="Body must be a JSON object")
        return data
    form = await request.form()
    return {k: v for k, v in form.items() if k not in {"csrf_token", "_method"}}


def _form_int_list(raw: list[Any]) -> list[int]:
    """Разобрать multi-value поле в список int (нецелые/пустые отбрасываются).

    Аналог ``mail-agregator _form_int_list`` для no-JS multi-select
    (``extra_team_ids[]`` / повторяющееся ``extra_team_ids``)."""
    out: list[int] = []
    for value in raw:
        text = str(value).strip()
        if not text:
            continue
        try:
            out.append(int(text))
        except ValueError:
            continue
    return out


def _actor_id(request: Request) -> int:
    return int(request.state.session.user_id)


def _client(request: Request) -> tuple[str, str]:
    return client_ip(request), request.headers.get("user-agent", "")


# --- Users ------------------------------------------------------------------


@router.get("/users")
async def list_users(db: DbSession) -> JSONResponse:
    return JSONResponse(content={"users": await AdminService(db).list_users()})


@router.post("/users")
async def create_user(request: Request, db: DbSession) -> JSONResponse:
    body = await _read_body(request)
    # no-JS форма (ADR-0012, docs/05 §4): доп. команды — multi-value поле
    # extra_team_ids[] / повторяющееся extra_team_ids (form.items() схлопывает
    # повторы → читаем getlist). JSON-путь принимает массив extra_team_ids как есть.
    ct = request.headers.get("content-type", "")
    if not ct.startswith("application/json"):
        form = await request.form()
        raw = [*form.getlist("extra_team_ids[]"), *form.getlist("extra_team_ids")]
        body["extra_team_ids"] = _form_int_list(raw)
    payload = CreateUserRequest.model_validate(body)
    ip, ua = _client(request)
    async with db.begin():
        user = await AdminService(db).create_user(
            actor_user_id=_actor_id(request),
            username=payload.username,
            display_name=payload.display_name,
            team_id=payload.team_id,
            extra_team_ids=payload.extra_team_ids,
            ip=ip,
            user_agent=ua,
        )
    return JSONResponse(
        status_code=201,
        content={
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "team_id": user.team_id,
        },
    )


@router.post("/users/{user_id}/reset")
async def reset_password(user_id: int, request: Request, db: DbSession) -> JSONResponse:
    ip, ua = _client(request)
    async with db.begin():
        await AdminService(db).reset_password(
            actor_user_id=_actor_id(request), target_id=user_id, ip=ip, user_agent=ua
        )
    return JSONResponse(content={"ok": True})


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, request: Request, db: DbSession) -> JSONResponse:
    ip, ua = _client(request)
    async with db.begin():
        await AdminService(db).delete_user(
            actor_user_id=_actor_id(request), target_id=user_id, ip=ip, user_agent=ua
        )
    return JSONResponse(content={"ok": True})


@router.patch("/users/{user_id}")
async def update_user(user_id: int, request: Request, db: DbSession) -> JSONResponse:
    body = await _read_body(request)
    payload = UpdateUserRequest.model_validate(body)
    ip, ua = _client(request)
    async with db.begin():
        user = await AdminService(db).update_user(
            actor_user_id=_actor_id(request),
            target_id=user_id,
            set_display_name="display_name" in body,
            display_name=payload.display_name,
            set_team="team_id" in body,
            team_id=payload.team_id,
            ip=ip,
            user_agent=ua,
        )
    return JSONResponse(
        content={
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "team_id": user.team_id,
        }
    )


# --- Доп. членство (ADR-0012) -----------------------------------------------


@router.post("/users/{user_id}/teams")
async def add_membership(user_id: int, request: Request, db: DbSession) -> JSONResponse:
    body = await _read_body(request)
    payload = AddMembershipRequest.model_validate(body)
    ip, ua = _client(request)
    async with db.begin():
        result = await AdminService(db).add_membership(
            actor_user_id=_actor_id(request),
            target_id=user_id,
            team_id=payload.team_id,
            ip=ip,
            user_agent=ua,
        )
    return JSONResponse(status_code=201, content=result)


@router.delete("/users/{user_id}/teams/{team_id}")
async def remove_membership(
    user_id: int, team_id: int, request: Request, db: DbSession
) -> Response:
    ip, ua = _client(request)
    async with db.begin():
        await AdminService(db).remove_membership(
            actor_user_id=_actor_id(request),
            target_id=user_id,
            team_id=team_id,
            ip=ip,
            user_agent=ua,
        )
    return Response(status_code=204)


# --- Teams ------------------------------------------------------------------


@router.get("/teams")
async def list_teams(db: DbSession) -> JSONResponse:
    teams_repo = TeamRepository(db)
    users_repo = UserRepository(db)
    teams = await teams_repo.list_all()
    ids = [t.id for t in teams]
    member_counts = await teams_repo.member_counts(ids)
    numbers_counts = await teams_repo.numbers_counts(ids)
    leader_ids = {t.leader_user_id for t in teams if t.leader_user_id is not None}
    leaders = {u.id: u for u in await users_repo.list_all() if u.id in leader_ids}
    items = [
        {
            "id": t.id,
            "name": t.name,
            "leader_user_id": t.leader_user_id,
            "leader_username": (
                leaders[t.leader_user_id].username
                if t.leader_user_id is not None and t.leader_user_id in leaders
                else None
            ),
            "members_count": member_counts.get(t.id, 0),
            "numbers_count": numbers_counts.get(t.id, 0),
            "is_active": t.is_active,
            "created_at": t.created_at.isoformat(),
        }
        for t in teams
    ]
    return JSONResponse(content={"teams": items})


@router.post("/teams")
async def create_team(request: Request, db: DbSession) -> JSONResponse:
    body = await _read_body(request)
    payload = CreateTeamRequest.model_validate(body)
    ip, ua = _client(request)
    async with db.begin():
        team = await TeamsService(db).create(
            actor_user_id=_actor_id(request), name=payload.name, ip=ip, user_agent=ua
        )
    return JSONResponse(status_code=201, content={"id": team.id, "name": team.name})


@router.patch("/teams/{team_id}")
async def rename_team(team_id: int, request: Request, db: DbSession) -> JSONResponse:
    body = await _read_body(request)
    payload = RenameTeamRequest.model_validate(body)
    ip, ua = _client(request)
    async with db.begin():
        team = await TeamsService(db).rename(
            actor_user_id=_actor_id(request),
            team_id=team_id,
            name=payload.name,
            ip=ip,
            user_agent=ua,
        )
    return JSONResponse(content={"id": team.id, "name": team.name})


@router.patch("/teams/{team_id}/leader")
async def set_team_leader(
    team_id: int, request: Request, db: DbSession
) -> JSONResponse:
    body = await _read_body(request)
    payload = SetLeaderRequest.model_validate(body)
    ip, ua = _client(request)
    async with db.begin():
        team = await TeamsService(db).set_leader(
            actor_user_id=_actor_id(request),
            team_id=team_id,
            new_leader_user_id=payload.new_leader_user_id,
            ip=ip,
            user_agent=ua,
        )
    return JSONResponse(content={"id": team.id, "leader_user_id": team.leader_user_id})


@router.delete("/teams/{team_id}")
async def delete_team(team_id: int, request: Request, db: DbSession) -> JSONResponse:
    ip, ua = _client(request)
    async with db.begin():
        await TeamsService(db).delete(
            actor_user_id=_actor_id(request), team_id=team_id, ip=ip, user_agent=ua
        )
    return JSONResponse(content={"ok": True})
