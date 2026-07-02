"""Cross-cutting middlewares: RequestID, SecurityHeaders, Session, MethodOverride.

Итоговая цепочка обработки (docs/03-architecture §Порядок middleware):
CSRF → MethodOverride → Session → SecurityHeaders → RequestID.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import unquote_plus

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.infrastructure.sessions import SessionData, SessionStore
from shared.config import get_settings
from shared.logging import get_logger

_log = get_logger(__name__)

_Scope = MutableMapping[str, Any]
_Message = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]
_ASGIApp = Callable[[_Scope, _Receive, _Send], Awaitable[None]]


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Базовые security-заголовки (docs/08-security §7)."""

    _CSP = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' https://telegram.org; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors https://telegram.org; "
        "base-uri 'self'"
    )
    _PERMISSIONS = "geolocation=(), camera=(), microphone=()"

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        is_html = ct.startswith("text/html")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if is_html:
            response.headers.setdefault("Content-Security-Policy", self._CSP)
            response.headers.setdefault("Referrer-Policy", "same-origin")
            response.headers.setdefault("Permissions-Policy", self._PERMISSIONS)
            response.headers.setdefault("Cache-Control", "no-store")
            if get_settings().cookie_secure:
                response.headers.setdefault(
                    "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
                )
        return response


class SessionMiddleware(BaseHTTPMiddleware):
    """Резолвит cookie ``sms_session`` в :class:`SessionData` (или None)."""

    def __init__(self, app: object) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._store = SessionStore()

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        session: SessionData | None = None
        token = request.cookies.get("sms_session")
        if token:
            session = await self._store.get(token)
            if session is not None:
                await self._store.touch(token, session)
        request.state.session = session
        request.state.session_token = token if session else None
        return await call_next(request)


# --- Method override (no-JS fallback) --------------------------------------

_OVERRIDE_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "/api/admin/users",
        "/api/admin/teams",
    }
)
_OVERRIDE_REGEX_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/admin/users/\d+$"),  # PATCH / DELETE override
    re.compile(r"^/api/admin/teams/\d+$"),  # PATCH / DELETE override
    re.compile(r"^/api/admin/teams/\d+/leader$"),  # PATCH override
    re.compile(r"^/api/admin/numbers/\d+$"),  # PATCH override (назначение команды)
    re.compile(r"^/api/numbers/\d+$"),  # DELETE override
)
_ALLOWED_OVERRIDE_METHODS: frozenset[str] = frozenset({"DELETE", "PATCH", "PUT"})


def _is_whitelisted_path(path: str) -> bool:
    if path in _OVERRIDE_EXACT_PATHS:
        return True
    return any(p.match(path) for p in _OVERRIDE_REGEX_PATHS)


def _extract_method_from_form(body: bytes, content_type: str) -> str | None:
    if "application/x-www-form-urlencoded" not in content_type.lower():
        return None
    try:
        text = body.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return None
    for pair in text.split("&"):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k == "_method":
            return unquote_plus(v).strip().upper()
    return None


class MethodOverrideMiddleware:
    """``POST + _method=<X>`` → эффективный метод ``<X>`` (ADR-0015-подобно)."""

    def __init__(self, app: _ASGIApp) -> None:
        self._app = app

    async def __call__(  # noqa: PLR0911
        self, scope: _Scope, receive: _Receive, send: _Send
    ) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return

        content_type = ""
        for raw_k, raw_v in scope.get("headers", []):
            if raw_k.lower() == b"content-type":
                content_type = raw_v.decode("latin-1")
                break
        if not content_type.lower().startswith("application/x-www-form-urlencoded"):
            await self._app(scope, receive, send)
            return

        body_chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                await self._app(scope, _replay(body_chunks, [message]), send)
                return
            body_chunks.append(message.get("body", b"") or b"")
            more = bool(message.get("more_body", False))
        body = b"".join(body_chunks)

        method_value = _extract_method_from_form(body, content_type)

        async def replay_receive() -> _Message:
            return {"type": "http.request", "body": body, "more_body": False}

        if not method_value:
            await self._app(scope, replay_receive, send)
            return

        path = scope.get("path", "") or ""
        if not _is_whitelisted_path(path):
            _log.info(
                "method_override_not_allowed", path=path, attempted_method=method_value
            )
            response = JSONResponse(
                status_code=400,
                content={
                    "error": "method_override_not_allowed",
                    "detail": "Method override не разрешён на этом маршруте.",
                },
            )
            await response(scope, replay_receive, send)
            return

        if method_value not in _ALLOWED_OVERRIDE_METHODS:
            await self._app(scope, replay_receive, send)
            return

        scope["method"] = method_value
        await self._app(scope, replay_receive, send)


def _replay(buffered: list[bytes], remaining: list[_Message]) -> _Receive:
    queue: list[_Message] = []
    for i, chunk in enumerate(buffered):
        queue.append(
            {
                "type": "http.request",
                "body": chunk,
                "more_body": (i < len(buffered) - 1) or bool(remaining),
            }
        )
    queue.extend(remaining)

    async def receive() -> _Message:
        if queue:
            return queue.pop(0)
        return {"type": "http.disconnect"}

    return receive
