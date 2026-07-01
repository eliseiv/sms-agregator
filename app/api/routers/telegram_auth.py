"""Telegram Mini App SSO endpoint (docs/05-api-contracts §3). CSRF-exempt."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError as PydanticValidationError

from app.api.cookies import set_session_cookies, set_tg_pending_cookie
from app.api.deps import DbSession
from app.api.schemas import TelegramAuthRequest, TelegramAuthResponse
from app.exceptions import ValidationError
from app.infrastructure.rate_limit import (
    LIMIT_TG_AUTH_IP,
    LIMIT_TG_AUTH_USER,
    client_ip,
    consume,
)
from app.infrastructure.repositories import UserRepository
from app.infrastructure.sessions import SessionStore
from app.application.telegram_sso_service import (
    InvalidInitDataError,
    TelegramSSOService,
)
from shared.config import get_settings
from shared.logging import get_logger

log = get_logger(__name__)
router = APIRouter()


def _invalid_init_data_response(code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": code, "detail": detail},
    )


@router.post("/api/telegram/auth")
async def telegram_auth(request: Request, db: DbSession) -> Response:
    settings = get_settings()
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")

    await consume(LIMIT_TG_AUTH_IP, f"ip:{ip}")

    try:
        body = await request.json()
    except ValueError as exc:
        raise ValidationError(detail="Body is not valid JSON") from exc
    try:
        payload = TelegramAuthRequest.model_validate(body)
    except PydanticValidationError as exc:
        raise ValidationError(detail="Invalid auth payload") from exc

    if not settings.telegram_bot_enabled:
        log.info("telegram_auth_bot_disabled")
        return _invalid_init_data_response(
            "invalid_init_data", "initData validation failed"
        )

    svc = TelegramSSOService(db)
    try:
        resolved = await svc.verify_and_resolve(payload.init_data)
    except InvalidInitDataError as exc:
        if exc.reason == "expired":
            return _invalid_init_data_response("init_data_expired", "initData expired")
        return _invalid_init_data_response(
            "invalid_init_data", "initData validation failed"
        )

    await consume(LIMIT_TG_AUTH_USER, f"tg:{resolved.telegram_user_id}")

    # Активная сессия → self-heal (без новой сессии/redirect).
    current = getattr(request.state, "session", None)
    if current is not None:
        await db.commit()  # закрыть autobegun read-tx
        healed = await svc.self_heal_link(
            telegram_user_id=resolved.telegram_user_id,
            user_id=current.user_id,
            ip=ip,
            user_agent=ua,
        )
        return JSONResponse(
            content=TelegramAuthResponse(linked=False, healed=healed).model_dump(
                exclude_none=True
            ),
            status_code=status.HTTP_200_OK,
        )

    if resolved.kind == "linked":
        assert resolved.user_id is not None
        user = await UserRepository(db).get_by_id(resolved.user_id)
        await db.commit()  # закрыть autobegun read-tx
        if user is None:
            async with db.begin():
                await svc.revoke_for_user(
                    user_id=resolved.user_id,
                    reason="link_user_missing",
                    ip=ip,
                    user_agent=ua,
                )
        else:
            async with db.begin():
                await UserRepository(db).record_login_success(user.id)
            session_token, csrf = await SessionStore().create(
                user.id, user.role, user.team_id, ip, ua
            )
            response: Response = JSONResponse(
                content=TelegramAuthResponse(linked=True, redirect="/").model_dump(
                    exclude_none=True
                ),
                status_code=status.HTTP_200_OK,
            )
            set_session_cookies(response, session_token, csrf, settings)
            log.info("telegram_auth_linked", user_id=user.id)
            return response

    # Unlinked (или устаревшая привязка) → pending token + cookie.
    token = await svc.create_pending(resolved.telegram_user_id)
    response = JSONResponse(
        content=TelegramAuthResponse(linked=False, redirect="/login").model_dump(
            exclude_none=True
        ),
        status_code=status.HTTP_200_OK,
    )
    set_tg_pending_cookie(response, token, settings)
    log.info("telegram_auth_unlinked_pending_set")
    return response
