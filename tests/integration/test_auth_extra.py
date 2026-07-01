"""Integration (coverage): logout, GET-страницы, привязка sms_tg_pending при логине."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.application.telegram_sso_service import TelegramSSOService
from app.core.security import hash_password
from shared.db import make_session
from tests.conftest import make_auth, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_logout_with_session_redirects_and_audits(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(s, username="lgroot", role="super_admin", team_id=None)
    cookies, headers = await make_auth(admin.id, "super_admin", None)
    r = await client.post("/logout", cookies=cookies, headers=headers)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    async with make_session() as s:
        cnt = (
            await s.execute(
                text("SELECT count(*) FROM admin_audit WHERE action='admin_logout'")
            )
        ).scalar()
    assert cnt == 1


async def test_login_page_get(client):
    r = await client.get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_login_page_redirects_when_authenticated(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(s, username="lpg", role="super_admin", team_id=None)
    cookies, _ = await make_auth(admin.id, "super_admin", None)
    r = await client.get("/login", cookies=cookies)
    assert r.status_code == 302
    assert r.headers["location"] == "/"


async def test_login_password_page_without_cookie_redirects(client):
    r = await client.get("/login/password")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_login_password_page_with_cookie(client):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "lpp-t")
            await seed_user(
                s,
                username="lppuser",
                role="group_leader",
                team_id=tid,
                password_hash=hash_password("pw12345678"),
            )
    r = await client.get("/login/password", cookies={"sms_login": "lppuser"})
    assert r.status_code == 200


async def test_set_password_page_without_cookie_redirects(client):
    r = await client.get("/set-password")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


async def test_login_links_pending_telegram(client):
    """Успешный логин при наличии sms_tg_pending → создаётся telegram_links."""
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "pl-t")
            u = await seed_user(
                s,
                username="pluser",
                role="group_leader",
                team_id=tid,
                password_hash=hash_password("pw-strong-123"),
            )
        # pending-токен на будущий telegram_user_id.
        token = await TelegramSSOService(s).create_pending(8080)
    r = await client.post(
        "/login/password",
        data={"password": "pw-strong-123"},
        cookies={"sms_login": "pluser", "sms_tg_pending": token},
    )
    assert r.status_code == 303
    async with make_session() as s:
        owner = (
            await s.execute(
                text("SELECT user_id FROM telegram_links WHERE telegram_user_id=8080")
            )
        ).scalar()
    assert owner == u.id
