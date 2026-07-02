"""Cookie-хелперы — единственное место, знающее имена cookie и атрибуты.

Имена (docs/05 §Соглашения): ``sms_session`` (HttpOnly), ``sms_csrf``
(читается JS), ``sms_setup``, ``sms_login``, ``sms_tg_pending`` (HttpOnly).
``Secure`` — при ``COOKIE_SECURE``.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response

from shared.config import Settings

SESSION_COOKIE = "sms_session"
CSRF_COOKIE = "sms_csrf"
SETUP_COOKIE = "sms_setup"
LOGIN_COOKIE = "sms_login"
TG_PENDING_COOKIE = "sms_tg_pending"
LOGGED_OUT_COOKIE = "sms_logged_out"

LOGIN_COOKIE_MAX_AGE = 15 * 60


def _domain(settings: Settings) -> str | None:
    return settings.COOKIE_DOMAIN or None


def set_session_cookies(
    response: Response, session_token: str, csrf: str, settings: Settings
) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_token,
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        domain=_domain(settings),
    )
    response.set_cookie(
        key=CSRF_COOKIE,
        value=csrf,
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        domain=_domain(settings),
    )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(key=SESSION_COOKIE, path="/", domain=_domain(settings))
    response.delete_cookie(key=CSRF_COOKIE, path="/", domain=_domain(settings))


def set_setup_cookie(response: Response, setup_token: str, settings: Settings) -> None:
    response.set_cookie(
        key=SETUP_COOKIE,
        value=setup_token,
        max_age=settings.SETUP_SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        domain=_domain(settings),
    )


def clear_setup_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(key=SETUP_COOKIE, path="/", domain=_domain(settings))


def set_login_cookie(response: Response, username: str, settings: Settings) -> None:
    if not username:
        return
    response.set_cookie(
        key=LOGIN_COOKIE,
        value=username,
        max_age=LOGIN_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        domain=_domain(settings),
    )


def clear_login_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(key=LOGIN_COOKIE, path="/", domain=_domain(settings))


def read_login_cookie(request: Request) -> str | None:
    raw = request.cookies.get(LOGIN_COOKIE)
    if not raw:
        return None
    cleaned = raw.strip().lower()
    return cleaned or None


def set_tg_pending_cookie(response: Response, token: str, settings: Settings) -> None:
    if not token:
        return
    response.set_cookie(
        key=TG_PENDING_COOKIE,
        value=token,
        max_age=settings.TG_PENDING_LINK_TTL_SECONDS,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        domain=_domain(settings),
    )


def clear_tg_pending_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(key=TG_PENDING_COOKIE, path="/", domain=_domain(settings))


def read_tg_pending_cookie(request: Request) -> str | None:
    raw = request.cookies.get(TG_PENDING_COOKIE)
    if not raw:
        return None
    cleaned = raw.strip()
    return cleaned or None


def set_logged_out_cookie(response: Response, settings: Settings) -> None:
    """Маркер «залипающего» выхода (ADR-0011): НЕ HttpOnly (читается ``tg.js``)."""
    response.set_cookie(
        key=LOGGED_OUT_COOKIE,
        value="1",
        max_age=settings.LOGOUT_STICKY_TTL_SECONDS,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        domain=_domain(settings),
    )


def clear_logged_out_cookie(response: Response, settings: Settings) -> None:
    """Сброс маркера выхода (Max-Age=0) — при установлении новой сессии/self-heal."""
    response.delete_cookie(key=LOGGED_OUT_COOKIE, path="/", domain=_domain(settings))


def read_logged_out_cookie(request: Request) -> bool:
    """True, если присутствует маркер ``sms_logged_out`` (значение ``1``)."""
    return request.cookies.get(LOGGED_OUT_COOKIE) == "1"
