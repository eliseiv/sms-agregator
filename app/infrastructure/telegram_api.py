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

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_json: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload_json["reply_markup"] = reply_markup
        try:
            async with self._build_client(timeout=30) as client:
                response = await client.post(
                    f"{self.base_url}/sendMessage",
                    json=payload_json,
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

    async def _call_method(
        self, method: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Вызвать произвольный Bot API-метод (деплой-операции). Не логирует токен."""
        try:
            async with self._build_client(timeout=30) as client:
                response = await client.post(f"{self.base_url}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise TelegramApiError(
                f"Telegram network error: {type(exc).__name__}"
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramApiError(
                f"Telegram {method}: invalid JSON, HTTP {response.status_code}"
            ) from exc
        if response.status_code < 400 and data.get("ok"):
            return data
        description = str(data.get("description") or response.reason_phrase or "")
        raise TelegramApiError(
            f"Telegram {method} failed: HTTP {response.status_code}: {description}"
        )

    async def set_webhook(self, *, url: str, secret_token: str) -> dict[str, Any]:
        """setWebhook с секрет-токеном (ADR-0010, деплой-операция)."""
        return await self._call_method(
            "setWebhook", {"url": url, "secret_token": secret_token}
        )

    async def set_my_commands(self, commands: list[dict[str, str]]) -> dict[str, Any]:
        """setMyCommands — меню бота (только /start, ADR-0010)."""
        return await self._call_method("setMyCommands", {"commands": commands})


_client: TelegramApiClient | None = None


def get_telegram_client(settings: Settings | None = None) -> TelegramApiClient:
    """Process-wide singleton клиента из настроек."""
    global _client
    if _client is None:
        s = settings or get_settings()
        _client = TelegramApiClient(s.TELEGRAM_BOT_TOKEN, s.TELEGRAM_PROXY_URL)
    return _client
