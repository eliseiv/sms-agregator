"""Просмотр входящих SMS — JSON ``/api/messages`` и SSR ``/messages`` (docs/05 §9).

Read-only (ADR-0014): ролевая видимость по текущей принадлежности номера +
cursor keyset-пагинация. Оба маршрута используют одну сервис-функцию
:class:`MessageQueryService`. Мутаций нет.

SSR-контекст ``/messages`` (стык с frontend ``messages.html``; имена переменных
консистентны с ``/app`` и ``/admin``, docs/05 §7):

- ``messages``: list[dict] — сериализованные SMS текущей страницы (``serialize_message``).
- ``next_cursor``: str | None — opaque-курсор следующей страницы (ссылка «Дальше»
  рендерится только при ``next_cursor != null``).
- ``numbers``: list[dict] — номера для ``<select>`` фильтра (``serialize_number``):
  для super_admin — все номера; для участника — номера его команд.
- ``teams``: list[{id, name}] — команды для ``<select>`` фильтра ``team_id``
  (только super_admin; для участника — пустой список, селектор команды не рендерится).
- ``is_super_admin``: bool — показывать ли селектор команды.
- ``to_number``: str | None — текущее значение фильтра номера (E.164).
- ``team_id``: int | None — текущее значение фильтра команды (super_admin).
- ``limit``: int — текущий размер страницы (дефолт 50, диапазон ``[1,100]``).
- ``csrf_token`` / ``username`` / ``display_name`` — как на прочих SSR-страницах.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.api.deps import (
    CurrentScope,
    CurrentSession,
    CurrentUser,
    DbSession,
    require_authenticated,
)
from app.api.serializers import serialize_message, serialize_number
from app.api.templates import render
from app.application.messages_service import DEFAULT_LIMIT, MessageQueryService
from app.exceptions import InvalidCursorError, InvalidLimitError
from app.infrastructure.repositories import PhoneNumberRepository, TeamRepository

# JSON API (docs/05 §9): GET /api/messages.
router = APIRouter(
    prefix="/api/messages",
    tags=["Messages"],
    dependencies=[Depends(require_authenticated)],
)

# SSR-страница (docs/05 §7, §9): GET /messages.
page_router = APIRouter(tags=["Messages UI"])


def _clean_str(value: str | None) -> str | None:
    """No-JS форма фильтра шлёт **пустые строки** для «Все номера»/«Все команды»
    (`to_number=`, `team_id=`). Семантика пустого значения — «фильтр не задан»
    (эквивалент отсутствию параметра), а НЕ фильтрация по `""` (иначе
    `to_number=""` дал бы 0 результатов). Пустое/пробельное → ``None``.
    """
    if value is None:
        return None
    value = value.strip()
    return value or None


def _clean_opt_int(value: str | None) -> int | None:
    """Опциональный целочисленный фильтр (`team_id`) из no-JS GET-формы.

    FastAPI не может привести пустую строку `team_id=""` к ``int`` → строгий
    `int | None`-параметр давал `422 validation_error` и ронял весь submit.
    Принимаем строкой и приводим сами (паттерн референса: пустое/непарсимое →
    ``None`` = фильтр не задан).
    """
    cleaned = _clean_str(value)
    if cleaned is None:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _clean_limit(value: str | None) -> int:
    """`limit` из no-JS формы: пустая/непарсимая строка → ``DEFAULT_LIMIT``
    (страница не падает `422` до хендлера). Валидное число (в т.ч. вне
    диапазона `[1,100]`) уходит в сервис, который проверяет диапазон и отдаёт
    `InvalidLimitError` → рендер `DEFAULT_LIMIT` (docs/05 §9).
    """
    cleaned = _clean_str(value)
    if cleaned is None:
        return DEFAULT_LIMIT
    try:
        return int(cleaned)
    except ValueError:
        return DEFAULT_LIMIT


@router.get("")
async def list_messages(
    db: DbSession,
    scope: CurrentScope,
    to_number: str | None = Query(default=None),
    team_id: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT),
) -> JSONResponse:
    """Список входящих SMS с cursor-пагинацией (docs/05 §9).

    ``limit`` намеренно **не** валидируется FastAPI-ограничениями (``ge/le``):
    диапазон ``[1,100]`` проверяет сервис, отдавая документированный
    ``400 invalid_limit`` (а не generic ``validation_error``).
    """
    page = await MessageQueryService(db).list_messages(
        is_super_admin=scope.is_super_admin,
        team_ids=scope.team_ids,
        to_number=to_number,
        team_id=team_id,
        cursor=cursor,
        limit=limit,
    )
    return JSONResponse(
        content={
            "messages": [serialize_message(sms) for sms in page.rows],
            "next_cursor": page.next_cursor,
        }
    )


@page_router.get("/messages", response_class=HTMLResponse)
async def messages_page(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    scope: CurrentScope,
    sess: CurrentSession,
    to_number: str | None = Query(default=None),
    team_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: str | None = Query(default=None),
) -> Response:
    """SSR-страница просмотра SMS (все роли). Первая (или ``cursor``) страница
    рендерится server-side теми же правилами, что ``GET /api/messages``.

    Query-параметры фильтра принимаются **строками** и валидируются здесь: своя
    no-JS GET-форма шлёт пустые строки (`to_number=`, `team_id=`, `limit=`) для
    состояний «Все номера»/«Все команды», а строгий `int`-параметр ронял их в
    `422 validation_error` (docs/05 §9, no-JS fallback обязателен). Пустое =
    «фильтр не задан» (не фильтрация по `""`).

    Битый ``cursor``/``limit`` в query (ADR-0014, docs/05 §9): страница
    показывает пустой список (без жёсткого 400) — API-семантика ошибок остаётся
    на ``GET /api/messages``. При невалидном ``limit`` в контекст рендера уходит
    ``DEFAULT_LIMIT`` (а не исходное битое значение), чтобы hidden-поле формы и
    следующий submit были валидны; ``cursor`` при этом просто игнорируется.
    """
    is_super_admin = scope.is_super_admin
    filter_to_number = _clean_str(to_number)
    filter_team_id = _clean_opt_int(team_id) if is_super_admin else None
    filter_cursor = _clean_str(cursor)
    render_limit = _clean_limit(limit)
    try:
        page = await MessageQueryService(db).list_messages(
            is_super_admin=is_super_admin,
            team_ids=scope.team_ids,
            to_number=filter_to_number,
            team_id=filter_team_id,
            cursor=filter_cursor,
            limit=render_limit,
        )
        messages = [serialize_message(sms) for sms in page.rows]
        next_cursor = page.next_cursor
    except InvalidCursorError:
        messages = []
        next_cursor = None
    except InvalidLimitError:
        messages = []
        next_cursor = None
        render_limit = DEFAULT_LIMIT

    numbers_repo = PhoneNumberRepository(db)
    teams_repo = TeamRepository(db)
    team_map = {t.id: t.name for t in await teams_repo.list_all()}

    # Набор номеров для селектора: super_admin — все; участник — свои команды.
    if is_super_admin:
        selector_numbers = await numbers_repo.list_all()
        teams = [{"id": tid, "name": name} for tid, name in team_map.items()]
        teams.sort(key=lambda t: str(t["name"]))
    elif scope.team_ids:
        selector_numbers = await numbers_repo.list_by_teams(scope.team_ids)
        teams = []
    else:
        selector_numbers = []
        teams = []

    numbers = [
        serialize_number(n, team_map.get(n.team_id) if n.team_id is not None else None)
        for n in selector_numbers
    ]

    context: dict[str, Any] = {
        "messages": messages,
        "next_cursor": next_cursor,
        "numbers": numbers,
        "teams": teams,
        "is_super_admin": is_super_admin,
        "to_number": filter_to_number,
        "team_id": filter_team_id,
        "limit": render_limit,
        "csrf_token": sess.csrf_token,
        "username": user.username,
        "display_name": user.display_name,
    }
    return await render(request, "messages.html", context)


# --- Страница «Номера» -------------------------------------------------------
# Управление номерами: никнейм (label), перенос в другую команду (super_admin),
# удаление. Контрол никнейма перенесён сюда с /messages (требование заказчика).
# Ролевой набор: super_admin — все номера; участник — номера своих команд.
@page_router.get(
    "/numbers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_authenticated)],
)
async def numbers_page(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    sess: CurrentSession,
) -> Response:
    """SSR-страница управления номерами (все роли).

    Мутации — существующие endpoints: ``PATCH /api/numbers/{id}`` (никнейм, §6),
    ``DELETE /api/numbers/{id}`` (удаление, §6), ``PATCH /api/admin/numbers/{id}``
    (перенос в команду — только super_admin, §4a). Набор номеров ролевой:
    super_admin — все; участник/лидер — номера своих команд.
    """
    numbers_repo = PhoneNumberRepository(db)
    teams_repo = TeamRepository(db)
    team_map = {t.id: t.name for t in await teams_repo.list_all()}
    if scope.is_super_admin:
        rows = await numbers_repo.list_all()
        teams_ctx = [{"id": tid, "name": name} for tid, name in team_map.items()]
        teams_ctx.sort(key=lambda t: str(t["name"]))
    elif scope.team_ids:
        rows = await numbers_repo.list_by_teams(scope.team_ids)
        teams_ctx = []
    else:
        rows = []
        teams_ctx = []
    numbers = [
        serialize_number(n, team_map.get(n.team_id) if n.team_id is not None else None)
        for n in rows
    ]
    context: dict[str, Any] = {
        "numbers": numbers,
        "teams": teams_ctx,
        "is_super_admin": scope.is_super_admin,
        "csrf_token": sess.csrf_token,
    }
    return await render(request, "numbers.html", context)
