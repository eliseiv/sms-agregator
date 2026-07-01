"""Лёгкая обёртка над stdlib logging со structlog-подобным API.

Позволяет писать ``log.info("event_name", key=value)`` без зависимости от
structlog. Значения ключей из ``REDACT_KEYS`` затираются ``[REDACTED]`` —
defense-in-depth против случайного логирования секретов (см. docs/08-security §9).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

REDACT_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "password_confirm",
        "csrf_token",
        "session_token",
        "setup_token",
        "Authorization",
        "authorization",
        "ADMIN_PASSWORD",
        "TELEGRAM_BOT_TOKEN",
        "TWILIO_AUTH_TOKEN",
        "DATABASE_URL",
        "REDIS_URL",
        "X-Twilio-Signature",
    }
)


def _fmt(event: str, kwargs: dict[str, Any]) -> str:
    if not kwargs:
        return event
    parts = []
    for key, value in kwargs.items():
        if key in REDACT_KEYS:
            value = "[REDACTED]"
        parts.append(f"{key}={value}")
    return f"{event} " + " ".join(parts)


class BoundLogger:
    """Минимальный structlog-подобный логгер поверх stdlib."""

    def __init__(self, logger: logging.Logger) -> None:
        self._log = logger

    def info(self, event: str, **kwargs: Any) -> None:
        self._log.info(_fmt(event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self._log.warning(_fmt(event, kwargs))

    def debug(self, event: str, **kwargs: Any) -> None:
        self._log.debug(_fmt(event, kwargs))

    def error(self, event: str, *, exc_info: bool = False, **kwargs: Any) -> None:
        self._log.error(_fmt(event, kwargs), exc_info=exc_info)

    def exception(self, event: str, **kwargs: Any) -> None:
        self._log.error(_fmt(event, kwargs), exc_info=True)


def configure_logging(level: str = "INFO", service: str = "api") -> None:
    """Настроить stdlib logging один раз при старте процесса."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )
    # Внешние логгеры логируют URL с токенами — приглушаем до WARNING.
    for noisy in ("twilio", "twilio.http_client", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("service").setLevel(log_level)
    del service


def get_logger(name: str | None = None) -> BoundLogger:
    return BoundLogger(logging.getLogger(name or "app"))
