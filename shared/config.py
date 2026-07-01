"""Типизированные настройки приложения из окружения (pydantic-settings).

Единый источник env-конфигурации (docs/07-deployment.md §4). Импортируется
api-процессом и фоновыми задачами. Секреты никогда не логируются.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide конфигурация из переменных окружения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- General ---
    SERVICE_NAME: str = "Twilio SMS Telegram Bot"
    APP_ENV: str = "production"
    PUBLIC_BASE_URL: str = "http://localhost:8137"
    LOG_LEVEL: str = "INFO"
    TIMEZONE: str = "Europe/Moscow"
    SAFE_REDIRECT_AFTER_LOGIN: str = "/"

    # --- Database / Redis ---
    DATABASE_URL: str = "postgresql+asyncpg://sms:CHANGE_ME@postgres:5432/sms"
    REDIS_URL: str = "redis://redis:6379/0"

    # --- Admin seed ---
    ADMIN_LOGIN: str = "admin"
    ADMIN_PASSWORD: str = ""

    # --- Sessions / auth ---
    SESSION_TTL_SECONDS: int = Field(default=1_209_600, ge=60)
    SESSION_ABSOLUTE_TTL_SECONDS: int = Field(default=2_592_000, ge=60)
    SETUP_SESSION_TTL_SECONDS: int = Field(default=900, ge=60)
    COOKIE_SECURE: bool = True
    COOKIE_DOMAIN: str | None = None
    LOGIN_FAILURE_THRESHOLD: int = Field(default=5, ge=1, le=100)
    LOGIN_LOCKOUT_MINUTES: int = Field(default=15, ge=1, le=1440)

    # --- Telegram Mini App SSO ---
    TG_AUTH_INIT_DATA_TTL_SECONDS: int = Field(default=300, ge=30, le=86_400)
    TG_PENDING_LINK_TTL_SECONDS: int = Field(default=900, ge=60, le=86_400)
    TG_MAX_LINKS_PER_USER: int = Field(default=10, ge=1, le=100)
    TELEGRAM_WEBAPP_URL: str = ""

    # --- Telegram Bot / delivery ---
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_PROXY_URL: str = ""
    DELIVERY_RETRY_INTERVAL_SECONDS: int = Field(default=60, ge=5, le=3600)
    DELIVERY_MAX_ATTEMPTS: int = Field(default=10, ge=1, le=100)

    # --- Twilio ---
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    VERIFY_TWILIO_SIGNATURE: bool = True
    TWILIO_SIGNATURE_HEADER: str = "X-Twilio-Signature"

    @model_validator(mode="after")
    def _enforce_required(self) -> Settings:
        """Required-in-prod проверки."""
        if self.is_prod and not self.ADMIN_PASSWORD:
            raise ValueError("Missing required env in production: ADMIN_PASSWORD")
        return self

    # --- Derived ---

    @property
    def is_prod(self) -> bool:
        return self.APP_ENV.lower() in {"production", "prod"}

    @property
    def cookie_secure(self) -> bool:
        """Флаг ``Secure`` на cookies — из COOKIE_SECURE (docs/08-security §2)."""
        return self.COOKIE_SECURE

    @property
    def telegram_bot_enabled(self) -> bool:
        """True только когда задан токен бота (без него HMAC не проверить)."""
        return bool(self.TELEGRAM_BOT_TOKEN)

    @property
    def public_base_url(self) -> str:
        return self.PUBLIC_BASE_URL.rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшированный singleton, чтобы повторные вызовы не парсили env заново."""
    return Settings()
