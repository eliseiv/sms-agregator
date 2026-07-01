"""Telegram Bot API клиент + типизированные ошибки доставки.

``TelegramForbiddenError`` — 403 / "bot was blocked" / "chat not found" —
привязка мертва (mark_dead). ``TelegramApiError`` — прочие ошибки (retry).
TLS verify включён по умолчанию (httpx). Не логирует токен.
"""

from __future__ import annotations

from typing import Any

import httpx

from shared.config import Settings, get_settings

# Подстроки в description Bot API, означающие «чат недоступен навсегда».
_FORBIDDEN_MARKERS = (
    "bot was blocked",
    "chat not found",
    "user is deactivated",
    "bot can't initiate conversation",
    "peer_id_invalid",
    "bots can't send messages to bots",
)


class TelegramApiError(RuntimeError):
    """Ретраибельная ошибка Bot API (сеть/5xx/прочее)."""


class TelegramForbiddenError(TelegramApiError):
    """Не-ретраибельная ошибка: чат заблокирован/не найден (403/400 blocked)."""


class TelegramApiClient:
    def __init__(self, token: str, proxy_url: str = "") -> None:
        self.token = token.strip()
        self.proxy_url = proxy_url.strip()
        self.base_url = (
            f"https://api.telegram.org/bot{self.token}" if self.token else ""
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.token)

    def _build_client(self, timeout: int) -> httpx.AsyncClient:
        if self.proxy_url:
            return httpx.AsyncClient(timeout=timeout, proxy=self.proxy_url, verify=True)
        return httpx.AsyncClient(timeout=timeout, verify=True)

    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        try:
            async with self._build_client(timeout=30) as client:
                response = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
        except httpx.HTTPError as exc:
            raise TelegramApiError(
                f"Telegram network error: {type(exc).__name__}"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramApiError(
                f"Telegram sendMessage: invalid JSON, HTTP {response.status_code}"
            ) from exc

        if response.status_code < 400 and payload.get("ok"):
            return payload

        description = str(payload.get("description") or response.reason_phrase or "")
        lowered = description.lower()
        if response.status_code == 403 or any(m in lowered for m in _FORBIDDEN_MARKERS):
            raise TelegramForbiddenError(
                f"Telegram sendMessage forbidden: HTTP {response.status_code}: {description}"
            )
        raise TelegramApiError(
            f"Telegram sendMessage failed: HTTP {response.status_code}: {description}"
        )


_client: TelegramApiClient | None = None


def get_telegram_client(settings: Settings | None = None) -> TelegramApiClient:
    """Process-wide singleton клиента из настроек."""
    global _client
    if _client is None:
        s = settings or get_settings()
        _client = TelegramApiClient(s.TELEGRAM_BOT_TOKEN, s.TELEGRAM_PROXY_URL)
    return _client
