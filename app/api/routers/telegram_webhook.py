"""Telegram webhook — приём апдейтов бота (docs/05-api-contracts §3a, ADR-0010).

Бот обрабатывает ТОЛЬКО ``/start`` → отправляет приветствие с кнопкой ``web_app``
(url = ``TELEGRAM_WEBAPP_URL``). Прочие апдейты → 200 no-op. CSRF-exempt;
аутентификация — секрет-токен ``X-Telegram-Bot-Api-Secret-Token`` (constant-time).
Тело апдейта и токены не логируются (docs/08 §9, §11).
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, Response

from app.infrastructure.rate_limit import LIMIT_TG_WEBHOOK_IP, client_ip, consume
from app.infrastructure.telegram_api import (
    TelegramApiError,
    get_telegram_client,
)
from shared.config import get_settings
from shared.logging import get_logger

log = get_logger(__name__)
router = APIRouter()

_WELCOME_TEXT = "Добро пожаловать! Откройте приложение по кнопке ниже."


def _webapp_markup(webapp_url: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Открыть приложение", "web_app": {"url": webapp_url}}]
        ]
    }


def _extract_start_chat_id(update: dict[str, Any]) -> int | None:
    """chat_id, если это message с text == '/start', иначе None."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if not isinstance(text, str) or text.strip() != "/start":
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    return int(chat_id) if isinstance(chat_id, int) else None


@router.post("/api/telegram/webhook")
async def telegram_webhook(request: Request) -> Response:
    settings = get_settings()
    await consume(LIMIT_TG_WEBHOOK_IP, f"ip:{client_ip(request)}")

    # Валидация секрет-токена ДО разбора тела (constant-time compare).
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    expected = settings.TELEGRAM_WEBHOOK_SECRET
    if not expected or not secrets.compare_digest(provided, expected):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": "invalid_webhook_secret", "detail": "Invalid secret"},
        )

    try:
        update = await request.json()
    except ValueError:
        # Некорректное тело — no-op 200 (Telegram не должен ретраить бесконечно).
        return JSONResponse(content={"ok": True})
    if not isinstance(update, dict):
        return JSONResponse(content={"ok": True})

    chat_id = _extract_start_chat_id(update)
    if chat_id is None:
        return JSONResponse(content={"ok": True})  # no-op для прочих апдейтов

    client = get_telegram_client(settings)
    if not client.is_configured:
        log.warning("tg_webhook_bot_not_configured")
        return JSONResponse(content={"ok": True})

    try:
        await client.send_message(
            chat_id,
            _WELCOME_TEXT,
            reply_markup=_webapp_markup(settings.TELEGRAM_WEBAPP_URL),
        )
        log.info("tg_webhook_start", chat_id=chat_id)
    except TelegramApiError:
        # Ошибка отправки не роняет обработчик (docs/05 §3a).
        log.warning("tg_webhook_send_failed", chat_id=chat_id)

    return JSONResponse(content={"ok": True})
