"""Webhook входящих SMS из Twilio (docs/05-api-contracts §1)."""

from __future__ import annotations

from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.api.deps import DbSession
from app.application.services import handle_incoming_sms
from app.infrastructure.telegram_api import get_telegram_client
from app.infrastructure.twilio_security import validate_twilio_signature
from shared.config import get_settings

router = APIRouter(tags=["Twilio Webhooks"])


def _public_request_url(request: Request) -> str:
    base = get_settings().public_base_url
    path = request.url.path
    query = request.url.query
    return f"{base}{path}?{query}" if query else f"{base}{path}"


@router.post("/api/webhooks/twilio/sms", summary="Webhook входящих SMS из Twilio")
async def twilio_sms_webhook(request: Request, db: DbSession) -> Response:
    settings = get_settings()
    raw_body = await request.body()
    payload = dict(parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True))

    if settings.VERIFY_TWILIO_SIGNATURE:
        if not settings.TWILIO_AUTH_TOKEN:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="TWILIO_AUTH_TOKEN не настроен",
            )
        signature = request.headers.get(settings.TWILIO_SIGNATURE_HEADER)
        if not validate_twilio_signature(
            auth_token=settings.TWILIO_AUTH_TOKEN,
            signature=signature,
            url=_public_request_url(request),
            form_data=payload,
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Неверная подпись Twilio",
            )

    await handle_incoming_sms(
        db,
        get_telegram_client(settings),
        settings,
        twilio_message_sid=payload.get("MessageSid"),
        from_number=payload.get("From", ""),
        to_number=payload.get("To", ""),
        body=payload.get("Body", ""),
        raw_payload=payload,
    )
    return Response(content="<Response></Response>", media_type="application/xml")
