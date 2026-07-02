"""Общая тестовая инфраструктура: env, БД (Postgres), Redis, ASGI-клиент, сиды.

Реальный PostgreSQL и Redis поднимаются в Docker (см. запуск qa). Внешние
Telegram Bot API и Twilio — мокаются. Схема строится alembic-миграцией
(реальные триггеры/CHECK/partial-UNIQUE).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

# --- ENV: выставить ДО импорта shared.config (get_settings кэшируется) --------
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Подключение к Postgres/Redis берётся из окружения. Fallback по умолчанию
# СОВПАДАЕТ с service-портами в .github/workflows/ci.yml (postgres 55620, redis
# 63811), поэтому CI-раннер проходит без доп. env. Локально (если порт занят)
# переопредели: TEST_DATABASE_URL / TEST_REDIS_URL. Никаких захардкоженных
# портов в коде — только дефолт, равный CI.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://sms:sms@localhost:55620/sms"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:63811/0")
TEST_BOT_TOKEN = "123456:AAExampleTestBotTokenForHMACVerification"


def sibling_sa_url(db_name: str) -> str:
    """SQLAlchemy-URL для соседней БД (smsmig/smsdata) на том же хосте/порту."""
    base, _ = TEST_DATABASE_URL.rsplit("/", 1)
    return f"{base}/{db_name}"


def sibling_dsn(db_name: str) -> str:
    """asyncpg-DSN для соседней БД (без +asyncpg-диалекта)."""
    return (
        sibling_sa_url(db_name)
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg://", "postgresql://")
    )


os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": TEST_DATABASE_URL,
        "REDIS_URL": TEST_REDIS_URL,
        "ADMIN_LOGIN": "admin",
        "ADMIN_PASSWORD": "admin-secret-pw",
        "COOKIE_SECURE": "false",
        "VERIFY_TWILIO_SIGNATURE": "false",
        "TELEGRAM_BOT_TOKEN": TEST_BOT_TOKEN,
        "TELEGRAM_PROXY_URL": "",
        "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret-xyz",
        "TELEGRAM_WEBAPP_URL": "https://example.test/app",
        "LOGIN_FAILURE_THRESHOLD": "3",
        "LOGIN_LOCKOUT_MINUTES": "15",
        "TG_AUTH_INIT_DATA_TTL_SECONDS": "300",
        "TG_MAX_LINKS_PER_USER": "3",
        "TIMEZONE": "Europe/Moscow",
        "LOG_LEVEL": "WARNING",
    }
)

import pytest  # noqa: E402
import redis.asyncio as redis_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import shared.models  # noqa: E402,F401  (регистрирует таблицы на Base.metadata)
from shared.config import get_settings  # noqa: E402
from shared.db import get_session_factory, make_session  # noqa: E402

_ALL_TABLES = (
    "deliveries",
    "inbound_sms",
    "phone_numbers",
    "telegram_links",
    "admin_audit",
    "service_state",
    "users",
    "teams",
)


def _run_alembic_upgrade(database_url: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head failed for {database_url}:\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )


@pytest.fixture(scope="session", autouse=True)
def _migrate_schema() -> None:
    """Построить схему основной тест-БД alembic-миграцией (один раз за сессию)."""
    _run_alembic_upgrade(TEST_DATABASE_URL)


@pytest.fixture(autouse=True)
async def _clean_state() -> None:
    """Очистить таблицы и Redis перед каждым тестом (изоляция)."""
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text("TRUNCATE " + ", ".join(_ALL_TABLES) + " RESTART IDENTITY CASCADE")
            )
    r = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
    await r.flushdb()
    await r.aclose()
    yield


# --- ASGI client -------------------------------------------------------------


@pytest.fixture
async def app():
    from app.main import create_app

    return create_app()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture
def settings():
    return get_settings()


# --- Redis helper ------------------------------------------------------------


@pytest.fixture
async def redis_conn():
    r = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
    yield r
    await r.aclose()


# --- Fake Telegram client ----------------------------------------------------


class FakeTelegram:
    """Мок TelegramApiClient: записывает вызовы; управляемое поведение."""

    def __init__(self, *, configured: bool = True, behavior=None) -> None:
        self._configured = configured
        self.calls: list[tuple[int, str]] = []
        self.markups: list[dict | None] = []
        # behavior: callable(chat_id, text) -> None | raises
        self._behavior = behavior

    @property
    def is_configured(self) -> bool:
        return self._configured

    async def send_message(self, chat_id: int, text: str, *, reply_markup=None):
        self.calls.append((chat_id, text))
        self.markups.append(reply_markup)
        if self._behavior is not None:
            self._behavior(chat_id, text)
        return {"ok": True}


# --- Seed helpers ------------------------------------------------------------


async def seed_team(session, name: str) -> int:
    from app.infrastructure.repositories import TeamRepository

    team = await TeamRepository(session).create(name=name, leader_user_id=None)
    return team.id


async def seed_user(
    session,
    *,
    username: str,
    role: str,
    team_id: int | None,
    password_hash: str | None = None,
    password_reset_required: bool = False,
    make_leader: bool = False,
):
    """Создать пользователя; при make_leader — назначить лидером его команды."""
    from app.infrastructure.repositories import TeamRepository, UserRepository

    users = UserRepository(session)
    user = await users.create(
        username=username,
        role="group_member" if role == "group_leader" else role,
        team_id=team_id,
        password_hash=password_hash,
        password_reset_required=password_reset_required,
    )
    if role == "group_leader" and team_id is not None:
        await TeamRepository(session).set_leader(
            team_id=team_id, leader_user_id=user.id
        )
        await users.update_fields(user.id, role="group_leader")
    return user


async def seed_link(session, *, telegram_user_id: int, user_id: int) -> None:
    from app.infrastructure.repositories import TelegramLinkRepository

    await TelegramLinkRepository(session).upsert(
        telegram_user_id=telegram_user_id, user_id=user_id
    )


async def seed_number(
    session, *, phone: str, team_id: int, added_by: int | None = None
):
    from app.infrastructure.repositories import PhoneNumberRepository

    return await PhoneNumberRepository(session).create(
        phone_number=phone, team_id=team_id, added_by_user_id=added_by, label=None
    )


async def make_session_cm():
    return make_session()


async def make_auth(user_id: int, role: str, team_id: int | None):
    """Создать серверную сессию; вернуть (cookies, headers) для клиента."""
    from app.infrastructure.sessions import SessionStore

    token, csrf = await SessionStore().create(
        user_id, role, team_id, "127.0.0.1", "pytest"
    )
    return {"sms_session": token}, {"X-CSRF-Token": csrf}


# --- initData HMAC builder ---------------------------------------------------


def build_init_data(
    *,
    telegram_user_id: int,
    auth_date: int,
    bot_token: str = TEST_BOT_TOKEN,
    first_name: str = "Test",
    username: str = "tester",
    valid_hash: bool = True,
) -> str:
    user_json = json.dumps(
        {"id": telegram_user_id, "first_name": first_name, "username": username},
        separators=(",", ":"),
    )
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAHtest",
        "user": user_json,
    }
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not valid_hash:
        digest = "0" * 64
    fields["hash"] = digest
    return urlencode(fields)
