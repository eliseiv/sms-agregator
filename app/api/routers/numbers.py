"""Numbers API — номера команды (docs/05-api-contracts §6).

Любой участник команды может добавлять/удалять номера своей команды.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import CurrentScope, DbSession, require_authenticated
from app.api.schemas import CreateNumberRequest, UpdateNumberRequest
from app.api.serializers import serialize_number
from app.application.audit import AuditWriter
from app.domain.services import normalize_phone
from app.exceptions import ApiError, ConflictError, NotFoundError
from app.infrastructure.rate_limit import client_ip
from app.infrastructure.repositories import PhoneNumberRepository, TeamRepository

router = APIRouter(
    prefix="/api/numbers",
    tags=["Numbers"],
    dependencies=[Depends(require_authenticated)],
)

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


async def _read_body(request: Request) -> dict[str, Any]:
    ct = request.headers.get("content-type", "")
    if ct.startswith("application/json"):
        data = await request.json()
        if not isinstance(data, dict):
            raise ApiError(
                "validation_error", "Body must be a JSON object", status_code=400
            )
        return data
    form = await request.form()
    return {k: v for k, v in form.items() if k not in {"csrf_token", "_method"}}


@router.get("")
async def list_numbers(
    db: DbSession, scope: CurrentScope, team_id: int | None = Query(default=None)
) -> JSONResponse:
    repo = PhoneNumberRepository(db)
    teams = TeamRepository(db)
    if scope.is_super_admin:
        numbers = (
            await repo.list_by_team(team_id)
            if team_id is not None
            else await repo.list_all()
        )
    else:
        # ADR-0012: номера всех команд участника (home ∪ доп.), не только home.
        if not scope.team_ids:
            return JSONResponse(content={"numbers": []})
        numbers = await repo.list_by_teams(scope.team_ids)
    team_map = {t.id: t.name for t in await teams.list_all()}
    items = [
        serialize_number(n, team_map.get(n.team_id) if n.team_id is not None else None)
        for n in numbers
    ]
    return JSONResponse(content={"numbers": items})


@router.post("")
async def create_number(
    request: Request, db: DbSession, scope: CurrentScope
) -> JSONResponse:
    body = await _read_body(request)
    payload = CreateNumberRequest.model_validate(body)

    # team_id: участник — любая из СВОИХ команд (scope.team_ids, ADR-0012);
    # super_admin обязан передать явно.
    if scope.is_super_admin:
        if payload.team_id is None:
            raise ApiError(
                "team_required", "super_admin обязан указать team_id", status_code=400
            )
        team_id = payload.team_id
    else:
        if payload.team_id is not None:
            if payload.team_id not in scope.team_ids:
                raise ApiError(
                    "forbidden",
                    "Нельзя добавить номер в чужую команду",
                    status_code=403,
                )
            team_id = payload.team_id
        elif scope.team_id is not None:
            # Дефолт — домашняя команда, если участник не выбрал явно.
            team_id = scope.team_id
        else:
            raise ApiError("forbidden", "У пользователя нет команды", status_code=403)

    normalized = normalize_phone(payload.phone_number)
    if not _E164_RE.match(normalized):
        raise ApiError(
            "invalid_phone_number",
            "Некорректный номер (ожидается E.164)",
            status_code=400,
        )

    repo = PhoneNumberRepository(db)
    teams = TeamRepository(db)
    if await teams.get(team_id) is None:
        raise NotFoundError("team_not_found", "Команда не найдена")
    if await repo.find_by_phone(normalized) is not None:
        raise ConflictError("phone_number_taken", "Номер уже привязан")

    # Закрыть autobegun read-tx (teams.get / find_by_phone) перед write-транзакцией.
    await db.commit()
    async with db.begin():
        number = await repo.create(
            phone_number=normalized,
            team_id=team_id,
            added_by_user_id=scope.user_id,
            label=payload.label,
        )
        await AuditWriter(db).log(
            actor_user_id=scope.user_id,
            action="number_added",
            details={"phone_number": normalized, "team_id": team_id},
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        )
    return JSONResponse(
        status_code=201,
        content={
            "id": number.id,
            "phone_number": number.phone_number,
            "team_id": number.team_id,
            "label": number.label,
        },
    )


@router.patch("/{number_id}")
async def update_number(
    number_id: int, request: Request, db: DbSession, scope: CurrentScope
) -> JSONResponse:
    """Задать/изменить/затереть никнейм номера (docs/05 §6, PATCH /api/numbers/{id}).

    Presence-семантика: ключ ``label`` в body отсутствует → no-op (возврат текущего
    состояния); присутствует непустой (после strip) → set; присутствует пустой/
    ``null`` → затирание (``label=NULL``). Guard принадлежности — по образцу
    ``delete_number``: super_admin — любой номер (вкл. unassigned); участник —
    только номер своей команды (``phone_numbers.team_id ∈ scope.team_ids``).
    """
    body = await _read_body(request)
    payload = UpdateNumberRequest.model_validate(body)
    set_label = "label" in body

    repo = PhoneNumberRepository(db)
    teams = TeamRepository(db)
    number = await repo.get(number_id)
    if number is None:
        raise NotFoundError("number_not_found", "Номер не найден")
    if not scope.is_super_admin and (
        number.team_id is None or number.team_id not in scope.team_ids
    ):
        raise ApiError(
            "forbidden", "Нельзя изменить номер чужой команды", status_code=403
        )

    team_name: str | None = None
    if number.team_id is not None:
        team = await teams.get(number.team_id)
        team_name = team.name if team is not None else None

    if set_label:
        # Закрыть autobegun read-tx (repo.get / teams.get) перед write-транзакцией.
        await db.commit()
        async with db.begin():
            await repo.set_label(number_id=number_id, label=payload.label)
            await AuditWriter(db).log(
                actor_user_id=scope.user_id,
                action="number_label_set",
                details={
                    "number_id": number_id,
                    "phone_number": number.phone_number,
                    "label": payload.label,
                },
                ip=client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
            )
        # expire_on_commit=False → объект живой; отражаем новое значение локально.
        number.label = payload.label

    return JSONResponse(content=serialize_number(number, team_name))


@router.delete("/{number_id}")
async def delete_number(
    number_id: int, request: Request, db: DbSession, scope: CurrentScope
) -> JSONResponse:
    repo = PhoneNumberRepository(db)
    number = await repo.get(number_id)
    if number is None:
        raise NotFoundError("number_not_found", "Номер не найден")
    # ADR-0012: участник управляет номерами любой своей команды (scope.team_ids).
    if not scope.is_super_admin and (
        number.team_id is None or number.team_id not in scope.team_ids
    ):
        raise ApiError(
            "forbidden", "Нельзя удалить номер чужой команды", status_code=403
        )

    # Закрыть autobegun read-tx (repo.get) перед write-транзакцией.
    await db.commit()
    async with db.begin():
        await repo.delete(number_id)
        await AuditWriter(db).log(
            actor_user_id=scope.user_id,
            action="number_removed",
            details={"phone_number": number.phone_number, "team_id": number.team_id},
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        )
    return JSONResponse(content={"ok": True})
