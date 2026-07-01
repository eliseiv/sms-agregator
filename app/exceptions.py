"""Доменные исключения + FastAPI-обработчики.

Формат ошибок API (docs/05-api-contracts §Соглашения): плоский JSON
``{"error": "<code>", "detail": "<человекочит.>"}`` + HTTP-код.
``NotAuthenticatedError``: для ``/api/*`` → 401 JSON, иначе → 302 ``/login``.
"""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ExceptionHandler

from shared.logging import get_logger

log = get_logger(__name__)


class ApiError(Exception):
    """База для доменных ошибок с плоским envelope ``{"error","detail"}``."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        code: str | None = None,
        detail: str | None = None,
        *,
        status_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.code = code or self.code
        self.detail = detail or self.code
        if status_code is not None:
            self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(self.detail)


class NotAuthenticatedError(ApiError):
    status_code = 401
    code = "not_authenticated"


class InvalidCredentialsError(ApiError):
    status_code = 401
    code = "invalid_credentials"


class ForbiddenError(ApiError):
    status_code = 403
    code = "forbidden"


class CSRFError(ApiError):
    status_code = 403
    code = "csrf_failed"


class NotFoundError(ApiError):
    status_code = 404
    code = "not_found"


class ConflictError(ApiError):
    status_code = 409
    code = "conflict"


class ValidationError(ApiError):
    status_code = 400
    code = "validation_error"


class AccountLockedError(ApiError):
    status_code = 423
    code = "account_locked"


class RateLimitedError(ApiError):
    status_code = 429
    code = "rate_limited"


class TelegramLinkLimitError(ApiError):
    status_code = 409
    code = "tg_link_limit"


class TelegramLinkOwnedByOtherError(ApiError):
    status_code = 409
    code = "tg_link_owned_by_other"


# --- Handlers ---------------------------------------------------------------


def _payload(err: ApiError) -> dict[str, str]:
    return {"error": err.code, "detail": err.detail}


def _headers(err: ApiError) -> dict[str, str]:
    return {"Retry-After": str(err.retry_after)} if err.retry_after is not None else {}


def _is_api(request: Request) -> bool:
    return (request.url.path or "").startswith("/api/")


def _domain_handler(request: Request, exc: ApiError) -> Response:
    log.info(
        "domain_error", code=exc.code, status=exc.status_code, path=request.url.path
    )
    if isinstance(exc, NotAuthenticatedError) and not _is_api(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    return JSONResponse(
        status_code=exc.status_code,
        content=_payload(exc),
        headers=_headers(exc),
    )


def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code_map = {
        401: "not_authenticated",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        429: "rate_limited",
    }
    code = code_map.get(exc.status_code, "http_error")
    log.info("http_exception", status=exc.status_code, path=request.url.path)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": code, "detail": str(exc.detail) if exc.detail else code},
    )


def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    field_paths = [
        {"loc": ".".join(str(p) for p in e.get("loc", ())), "type": e.get("type")}
        for e in exc.errors()
    ]
    log.info("validation_error", path=request.url.path, errors=field_paths)
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": "validation_error", "detail": "Request validation failed"},
    )


def _pydantic_validation_handler(
    request: Request, exc: PydanticValidationError
) -> JSONResponse:
    """Ручной ``Model.model_validate`` в роутерах бросает pydantic.ValidationError.

    Без этого обработчика он попал бы в общий Exception-handler → 500.
    Конвертируем в задокументированный 400 ``validation_error``.
    """
    log.info(
        "pydantic_validation_error", path=request.url.path, errors=exc.error_count()
    )
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": "validation_error", "detail": "Request validation failed"},
    )


def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log.error(
        "unhandled_exception",
        path=request.url.path,
        exc_type=type(exc).__name__,
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_error", "detail": "An internal error occurred."},
    )


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, cast(ExceptionHandler, _domain_handler))
    app.add_exception_handler(
        StarletteHTTPException, cast(ExceptionHandler, _http_handler)
    )
    app.add_exception_handler(
        RequestValidationError, cast(ExceptionHandler, _validation_handler)
    )
    app.add_exception_handler(
        PydanticValidationError, cast(ExceptionHandler, _pydantic_validation_handler)
    )
    app.add_exception_handler(Exception, _unhandled_handler)
