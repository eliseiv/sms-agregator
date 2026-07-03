"""Twilio REST-клиент для входящих номеров аккаунта (ADR-0013).

Официальный Twilio SDK **синхронный** — вызывать только из threadpool
(``asyncio.to_thread``), иначе блокирует event loop (ADR-0013 §Consequences).
Проходит все страницы (пагинация). TLS включён (дефолт SDK). Секреты (SID/token)
не логируются.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests  # type: ignore[import-untyped]  # transitive-зависимость twilio
from twilio.base.exceptions import (  # type: ignore[import-not-found, import-untyped]
    TwilioException,
)
from twilio.http.http_client import (  # type: ignore[import-not-found, import-untyped]
    TwilioHttpClient,
)
from twilio.rest import Client  # type: ignore[import-not-found, import-untyped]

from shared.config import Settings, get_settings

# Размер страницы Twilio REST (ADR-0013): аккаунт ~сотни номеров, PageSize 100.
_PAGE_SIZE = 100
# Таймаут HTTP-запроса к Twilio (сек) — отказоустойчивость (ADR-0013 §4).
_HTTP_TIMEOUT_SECONDS = 30


class TwilioNotConfiguredError(RuntimeError):
    """``TWILIO_ACCOUNT_SID``/``TWILIO_AUTH_TOKEN`` не заданы (→ 503)."""


class TwilioNumbersApiError(RuntimeError):
    """Сбой Twilio API: сеть, 5xx, таймаут, аутентификация (→ 502/503)."""


@dataclass(frozen=True, slots=True)
class TwilioNumber:
    phone_number: str
    friendly_name: str | None


class TwilioNumbersClient:
    """Тянет входящие номера Twilio-аккаунта (``IncomingPhoneNumbers``)."""

    def __init__(self, account_sid: str, auth_token: str) -> None:
        self._sid = (account_sid or "").strip()
        self._token = (auth_token or "").strip()

    @property
    def is_configured(self) -> bool:
        return bool(self._sid and self._token)

    def list_incoming_numbers(self) -> list[TwilioNumber]:
        """Синхронный вызов Twilio REST — **только из threadpool** (ADR-0013).

        ``.list(page_size=...)`` внутри SDK обходит **все** страницы (пагинация)
        и возвращает полный набор. Сбой сети/аутентификации/5xx/таймаут →
        :class:`TwilioNumbersApiError`.
        """
        if not self.is_configured:
            raise TwilioNotConfiguredError(
                "TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN не сконфигурированы"
            )
        http_client = TwilioHttpClient(timeout=_HTTP_TIMEOUT_SECONDS)
        client = Client(self._sid, self._token, http_client=http_client)
        try:
            records = client.incoming_phone_numbers.list(page_size=_PAGE_SIZE)
        except TwilioException as exc:
            raise TwilioNumbersApiError(
                f"Twilio API error: {type(exc).__name__}"
            ) from exc
        except requests.RequestException as exc:
            raise TwilioNumbersApiError(
                f"Twilio network error: {type(exc).__name__}"
            ) from exc

        numbers: list[TwilioNumber] = []
        for rec in records:
            phone = getattr(rec, "phone_number", None)
            if not phone:
                continue
            friendly = getattr(rec, "friendly_name", None)
            numbers.append(
                TwilioNumber(
                    phone_number=str(phone),
                    friendly_name=str(friendly) if friendly else None,
                )
            )
        return numbers


def get_twilio_numbers_client(settings: Settings | None = None) -> TwilioNumbersClient:
    """Собрать клиент из настроек (те же секреты, что для подписи вебхука)."""
    s = settings or get_settings()
    return TwilioNumbersClient(s.TWILIO_ACCOUNT_SID, s.TWILIO_AUTH_TOKEN)
