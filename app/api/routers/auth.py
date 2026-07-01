"""Двухэтапный логин + set-password + logout (docs/05-api-contracts §2, SSR)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError as PydanticValidationError

from app.api.cookies import (
    clear_login_cookie,
    clear_session_cookies,
    clear_setup_cookie,
    clear_tg_pending_cookie,
    read_login_cookie,
    read_tg_pending_cookie,
    set_login_cookie,
    set_session_cookies,
    set_setup_cookie,
)
from app.api.deps import DbSession
from app.api.schemas import (
    LoginPasswordRequest,
    LoginUsernameRequest,
    SetPasswordRequest,
)
from app.api.templates import render
from app.application.auth_service import AuthService
from app.application.telegram_sso_service import TelegramSSOService
from app.exceptions import NotAuthenticatedError
from app.infrastructure.rate_limit import (
    LIMIT_LOGIN,
    LIMIT_LOGIN_USERNAME,
    LIMIT_SET_PASSWORD,
    client_ip,
    consume,
)
from app.infrastructure.repositories import UserRepository
from app.infrastructure.sessions import SetupSessionStore
from shared.config import get_settings
from shared.logging import get_logger

log = get_logger(__name__)
router = APIRouter()


async def _link_tg_if_pending(
    request: Request,
    response: Response,
    db: DbSession,
    *,
    user_id: int,
    ip: str,
    ua: str | None,
) -> None:
    """Погасить ``sms_tg_pending`` (если есть) и привязать telegram_links.

    Вызывать внутри активной транзакции. Cookie очищается всегда.
    """
    settings = get_settings()
    token = read_tg_pending_cookie(request)
    if not token:
        return
    svc = TelegramSSOService(db)
    telegram_user_id = await svc.consume_pending(token)
    clear_tg_pending_cookie(response, settings)
    if telegram_user_id is None:
        return
    await svc.link_pending(
        telegram_user_id=telegram_user_id, user_id=user_id, ip=ip, user_agent=ua
    )


# --- Шаг 1: логин -----------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if getattr(request.state, "session", None) is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    return await render(
        request, "login.html", {"csrf_token": "", "error_message": None}
    )


@router.post("/login")
async def login_username_submit(request: Request) -> Response:
    """Шаг-1 логина. docs/08-security §6: ВСЕГДА 303 → /login/password + sms_login,

    независимо от существования/состояния аккаунта (анти-энумерация). Логика
    set-password/сброса пароля разрешается на шаге-2 в ``AuthService.login``.
    """
    settings = get_settings()
    ip = client_ip(request)
    form = await request.form()
    try:
        payload = LoginUsernameRequest.model_validate(
            {"username": str(form.get("username", "") or "").strip().lower()}
        )
    except PydanticValidationError:
        return await render(
            request,
            "login.html",
            {"csrf_token": "", "error_message": "Введите логин (3-64 символа)."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Rate-limit per IP + per username (docs/08-security §4).
    await consume(LIMIT_LOGIN_USERNAME, f"ip:{ip}")
    await consume(LIMIT_LOGIN_USERNAME, f"user:{payload.username}")

    # Единый ответ для любого kind — без обращения к БД, без ветвлений.
    response = RedirectResponse(
        url="/login/password", status_code=status.HTTP_303_SEE_OTHER
    )
    set_login_cookie(response, payload.username, settings)
    return response


# --- Шаг 2: пароль ----------------------------------------------------------


@router.get("/login/password", response_class=HTMLResponse)
async def login_password_page(request: Request) -> Response:
    if getattr(request.state, "session", None) is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    username = read_login_cookie(request)
    if not username:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return await render(
        request,
        "login_password.html",
        {"csrf_token": "", "username": username, "error_message": None},
    )


async def _render_password_error(
    request: Request, *, username: str, error_message: str, status_code: int
) -> Response:
    return await render(
        request,
        "login_password.html",
        {"csrf_token": "", "username": username, "error_message": error_message},
        status_code=status_code,
    )


@router.post("/login/password")
async def login_password_submit(request: Request, db: DbSession) -> Response:  # noqa: PLR0911
    settings = get_settings()
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")

    username = read_login_cookie(request)
    if not username:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    form = await request.form()
    try:
        payload = LoginPasswordRequest.model_validate(
            {"password": str(form.get("password", "") or ""), "csrf_token": None}
        )
    except PydanticValidationError:
        return await _render_password_error(
            request,
            username=username,
            error_message="Введите пароль.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    await consume(LIMIT_LOGIN, f"{username}|{ip}")

    svc = AuthService(db)
    async with db.begin():
        result = await svc.login(
            username=username, password=payload.password, ip=ip, user_agent=ua
        )

    if result.kind == "locked":
        retry_sec = result.retry_after_sec or 0
        minutes = max(1, (retry_sec + 59) // 60)
        return await _render_password_error(
            request,
            username=username,
            error_message=f"Слишком много неудачных попыток. Повторите через {minutes} мин.",
            status_code=status.HTTP_423_LOCKED,
        )

    if result.kind == "invalid":
        return await _render_password_error(
            request,
            username=username,
            error_message="Неверный логин или пароль.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    if result.kind == "set_password_required":
        assert result.setup_token is not None
        response: Response = RedirectResponse(
            url="/set-password", status_code=status.HTTP_303_SEE_OTHER
        )
        set_setup_cookie(response, result.setup_token, settings)
        clear_login_cookie(response, settings)
        return response

    # session_created
    assert result.session_token and result.csrf
    response = RedirectResponse(
        url=settings.SAFE_REDIRECT_AFTER_LOGIN, status_code=status.HTTP_303_SEE_OTHER
    )
    set_session_cookies(response, result.session_token, result.csrf, settings)
    clear_login_cookie(response, settings)
    if result.user_id is not None:
        async with db.begin():
            await _link_tg_if_pending(
                request, response, db, user_id=result.user_id, ip=ip, ua=ua
            )
    return response


# --- set-password -----------------------------------------------------------


@router.get("/set-password", response_class=HTMLResponse)
async def set_password_page(request: Request, db: DbSession) -> Response:
    setup_token = request.cookies.get("sms_setup")
    if not setup_token:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    setup = await SetupSessionStore().get(setup_token)
    if setup is None:
        response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
        clear_setup_cookie(response, get_settings())
        return response
    user = await UserRepository(db).get_by_id(setup.user_id)
    return await render(
        request,
        "set_password.html",
        {
            "csrf_token": setup.csrf_token,
            "username": user.username if user else "",
            "error_message": None,
        },
    )


async def _render_set_password_error(
    request: Request,
    db: DbSession,
    *,
    setup_token: str,
    error_message: str,
    status_code: int,
) -> Response:
    setup = await SetupSessionStore().get(setup_token)
    if setup is None:
        response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        clear_setup_cookie(response, get_settings())
        return response
    user = await UserRepository(db).get_by_id(setup.user_id)
    return await render(
        request,
        "set_password.html",
        {
            "csrf_token": setup.csrf_token,
            "username": user.username if user else "",
            "error_message": error_message,
        },
        status_code=status_code,
    )


@router.post("/set-password")
async def set_password_submit(request: Request, db: DbSession) -> Response:  # noqa: PLR0911
    settings = get_settings()
    setup_token = request.cookies.get("sms_setup")
    if not setup_token:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    form = await request.form()
    password = str(form.get("password", "") or "")
    password_confirm = str(form.get("password_confirm", "") or "")

    await consume(LIMIT_SET_PASSWORD, f"setup:{setup_token}")

    if password != password_confirm:
        return await _render_set_password_error(
            request,
            db,
            setup_token=setup_token,
            error_message="Пароли не совпадают.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        SetPasswordRequest.model_validate(
            {
                "password": password,
                "password_confirm": password_confirm,
                "csrf_token": "",
            }
        )
    except PydanticValidationError:
        return await _render_set_password_error(
            request,
            db,
            setup_token=setup_token,
            error_message="Пароль слишком короткий: минимум 8 символов.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    svc = AuthService(db)
    try:
        async with db.begin():
            result = await svc.complete_set_password(
                setup_token=setup_token, password=password, ip=ip, user_agent=ua
            )
    except NotAuthenticatedError:
        response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        clear_setup_cookie(response, settings)
        return response

    assert result.session_token and result.csrf
    response = RedirectResponse(
        url=settings.SAFE_REDIRECT_AFTER_LOGIN, status_code=status.HTTP_302_FOUND
    )
    clear_setup_cookie(response, settings)
    clear_login_cookie(response, settings)
    set_session_cookies(response, result.session_token, result.csrf, settings)
    if result.user_id is not None:
        async with db.begin():
            await _link_tg_if_pending(
                request, response, db, user_id=result.user_id, ip=ip, ua=ua
            )
    return response


# --- logout -----------------------------------------------------------------


@router.post("/logout")
async def logout(request: Request, db: DbSession) -> Response:
    settings = get_settings()
    sess = getattr(request.state, "session", None)
    token: str | None = getattr(request.state, "session_token", None)
    if sess is None or token is None:
        response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
        clear_session_cookies(response, settings)
        clear_login_cookie(response, settings)
        return response

    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    async with db.begin():
        await AuthService(db).logout(
            session_token=token,
            actor_user_id=sess.user_id,
            is_admin=(sess.role == "super_admin"),
            ip=ip,
            user_agent=ua,
        )
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    clear_session_cookies(response, settings)
    clear_login_cookie(response, settings)
    return response
