"""Admin numbers API — unassigned-пул и распределение (docs/05-api-contracts §4a).

Все endpoints — ``require_admin`` (super_admin). Распределение произвольной
команды — привилегия админа (ADR-0009); участник управляет своей командой
через ``/api/numbers`` (§6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import DbSession, require_admin
from app.api.serializers import serialize_number
from app.application.audit import AuditWriter
from app.application.twilio_sync_service import (
    SyncResult,
    sync_twilio_numbers_to_pool,
)
from app.exceptions import ApiError, NotFoundError, ValidationError
from app.infrastructure.rate_limit import client_ip
from app.infrastructure.repositories import PhoneNumberRepository, TeamRepository
from app.infrastructure.twilio_numbers import (
    TwilioNotConfiguredError,
    TwilioNumbersApiError,
    get_twilio_numbers_client,
)

router = APIRouter(
    prefix="/api/admin/numbers",
    tags=["Admin Numbers"],
    dependencies=[Depends(require_admin)],
)

_ASSIGNMENT_VALUES = frozenset({"assigned", "unassigned", "all"})


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


def _actor_id(request: Request) -> int:
    return int(request.state.session.user_id)


@router.get("")
async def list_numbers(
    db: DbSession,
    assignment: str = Query(default="all"),
    team_id: int | None = Query(default=None),
) -> JSONResponse:
    if assignment not in _ASSIGNMENT_VALUES:
        raise ApiError(
            "invalid_query",
            "assignment должен быть assigned|unassigned|all",
            status_code=400,
        )
    # team_id + assignment=unassigned логически несовместимы (§4a).
    if team_id is not None and assignment == "unassigned":
        raise ApiError(
            "invalid_query",
            "team_id несовместим с assignment=unassigned",
            status_code=400,
        )

    repo = PhoneNumberRepository(db)
    teams = TeamRepository(db)
    numbers = await repo.list_filtered(assignment=assignment, team_id=team_id)
    team_map = {t.id: t.name for t in await teams.list_all()}
    items = [
        serialize_number(n, team_map.get(n.team_id) if n.team_id is not None else None)
        for n in numbers
    ]
    return JSONResponse(content={"numbers": items})


@router.patch("/{number_id}")
async def assign_number_team(
    number_id: int, request: Request, db: DbSession
) -> JSONResponse:
    body = await _read_body(request)
    # team_id: int|null. Отсутствие ключа трактуем как ошибку валидации.
    if "team_id" not in body:
        raise ValidationError(detail="team_id is required")
    raw = body["team_id"]
    new_team_id: int | None
    if raw is None or raw == "":
        new_team_id = None
    else:
        try:
            new_team_id = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValidationError(detail="team_id must be int or null") from exc

    repo = PhoneNumberRepository(db)
    teams = TeamRepository(db)
    number = await repo.get(number_id)
    if number is None:
        raise NotFoundError("number_not_found", "Номер не найден")

    team_name: str | None = None
    if new_team_id is not None:
        team = await teams.get(new_team_id)
        if team is None:
            raise NotFoundError("team_not_found", "Команда не найдена")
        team_name = team.name

    previous_team_id = number.team_id
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")

    # Закрыть autobegun read-tx (repo.get / teams.get) перед write-транзакцией.
    await db.commit()
    async with db.begin():
        await repo.set_team(number_id=number_id, team_id=new_team_id)
        await AuditWriter(db).log(
            actor_user_id=_actor_id(request),
            action="number_team_assigned",
            details={
                "number_id": number_id,
                "phone_number": number.phone_number,
                "previous_team_id": previous_team_id,
                "new_team_id": new_team_id,
            },
            ip=ip,
            user_agent=ua,
        )
    return JSONResponse(
        content={
            "id": number_id,
            "phone_number": number.phone_number,
            "team_id": new_team_id,
            "team_name": team_name,
        }
    )


@router.post("/sync")
async def sync_numbers(request: Request, db: DbSession) -> JSONResponse:
    """On-demand sync входящих номеров Twilio в unassigned-пул (ADR-0013, §4a).

    ``require_admin`` (router-level) + CSRF (middleware). Тянет все номера
    аккаунта (пагинация), upsert как unassigned ``ON CONFLICT DO NOTHING``.
    """
    client = get_twilio_numbers_client()
    if not client.is_configured:
        raise ApiError(
            "twilio_not_configured",
            "TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN не сконфигурированы",
            status_code=503,
        )

    actor_id = _actor_id(request)
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")

    async def _audit(result: SyncResult) -> None:
        await AuditWriter(db).log(
            actor_user_id=actor_id,
            action="numbers_synced",
            details={
                "synced_total": result.synced_total,
                "added": result.added,
                "skipped_existing": result.skipped_existing,
            },
            ip=ip,
            user_agent=ua,
        )

    try:
        result = await sync_twilio_numbers_to_pool(db, client, audit=_audit)
    except TwilioNotConfiguredError as exc:
        raise ApiError(
            "twilio_not_configured",
            "TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN не сконфигурированы",
            status_code=503,
        ) from exc
    except TwilioNumbersApiError as exc:
        raise ApiError(
            "twilio_error",
            "Сбой Twilio API при синхронизации номеров",
            status_code=502,
        ) from exc

    return JSONResponse(
        content={
            "synced_total": result.synced_total,
            "added": result.added,
            "skipped_existing": result.skipped_existing,
        }
    )
