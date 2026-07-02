"""CSRF middleware: double-submit cookie + серверная сверка (docs/08-security §3).

Для ``POST/PUT/PATCH/DELETE`` (кроме exempt) токен из заголовка ``X-CSRF-Token``
или поля ``csrf_token`` сверяется с токеном сессии. ``/set-password`` использует
setup-сессию (``sms_setup``), остальное — основную (``sms_session``).

Exempt (защита — криптоподпись запроса / отсутствие сессии на этом шаге):
webhook Twilio, ``/api/telegram/auth``, шаги логина (сессии ещё нет), ``/health``.
"""

from __future__ import annotations

import secrets
from urllib.parse import unquote_plus

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.exceptions import CSRFError
from app.infrastructure.sessions import SessionStore, SetupSessionStore
from shared.logging import get_logger

_log = get_logger(__name__)

SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/login",  # шаг-1: сессии ещё нет (защита — rate-limit)
        "/login/password",  # шаг-2: сессии ещё нет (защита — rate-limit + lockout)
        "/health",
        "/api/telegram/auth",  # защита — HMAC initData
        "/api/telegram/webhook",  # защита — секрет-токен X-Telegram-Bot-Api-Secret-Token
        "/api/webhooks/twilio/sms",  # защита — подпись Twilio
    }
)


def _is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS


def _extract_token_from_form(form_body: bytes, content_type: str) -> str | None:
    if "application/x-www-form-urlencoded" not in content_type.lower():
        return None
    try:
        text = form_body.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return None
    for pair in text.split("&"):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k == "csrf_token":
            return unquote_plus(v)
    return None


class CSRFMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._sessions = SessionStore()
        self._setup = SetupSessionStore()

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        try:
            await self._verify(request)
        except CSRFError as exc:
            _log.info("csrf_failed", status=exc.status_code, path=request.url.path)
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": exc.code, "detail": exc.detail},
            )
        return await call_next(request)

    async def _verify(self, request: Request) -> None:
        if request.method in SAFE_METHODS or _is_exempt(request.url.path):
            return

        is_set_password = request.url.path == "/set-password"

        header_token = request.headers.get("X-CSRF-Token")
        body_token: str | None = None
        if not header_token:
            content_type = request.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in content_type.lower():
                body_bytes = await request.body()
                body_token = _extract_token_from_form(body_bytes, content_type)

        submitted = header_token or body_token
        if not submitted:
            raise CSRFError(detail="Missing CSRF token")

        expected: str | None = None
        if is_set_password:
            setup_token = request.cookies.get("sms_setup")
            if setup_token:
                ss = await self._setup.get(setup_token)
                if ss is not None:
                    expected = ss.csrf_token
        else:
            session_token = request.cookies.get("sms_session")
            if session_token:
                sd = await self._sessions.get(session_token)
                if sd is not None:
                    expected = sd.csrf_token

        if expected is None:
            raise CSRFError(detail="No active session for CSRF check")
        if not secrets.compare_digest(expected, submitted):
            raise CSRFError(detail="CSRF token mismatch")
