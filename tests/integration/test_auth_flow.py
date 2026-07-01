"""Integration: seed_admin, двухэтапный логин, lockout, set-password (docs/06 §Auth)."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.application.auth_service import seed_admin
from app.core.security import hash_password
from shared.config import get_settings
from shared.db import make_session
from tests.conftest import seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


# --- seed_admin idempotency -------------------------------------------------


async def test_seed_admin_created_then_unchanged():
    async with make_session() as s:
        async with s.begin():
            r1 = await seed_admin(s)
    assert r1 == "created"
    async with make_session() as s:
        async with s.begin():
            r2 = await seed_admin(s)
    assert r2 == "unchanged"
    async with make_session() as s:
        cnt = (
            await s.execute(text("SELECT count(*) FROM users WHERE role='super_admin'"))
        ).scalar()
    assert cnt == 1


async def test_seed_admin_rename_keeps_single_super_admin():
    async with make_session() as s:
        async with s.begin():
            await seed_admin(s)
    old = os.environ["ADMIN_LOGIN"]
    try:
        os.environ["ADMIN_LOGIN"] = "root2"
        get_settings.cache_clear()
        async with make_session() as s:
            async with s.begin():
                r = await seed_admin(s)
        assert r == "updated"
        async with make_session() as s:
            rows = (
                await s.execute(
                    text("SELECT username FROM users WHERE role='super_admin'")
                )
            ).all()
        assert [x[0] for x in rows] == ["root2"]
    finally:
        os.environ["ADMIN_LOGIN"] = old
        get_settings.cache_clear()


# --- Двухэтапный логин ------------------------------------------------------


async def _seed_login_user(username: str, password: str, *, reset: bool = False):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "team-" + username)
            await seed_user(
                s,
                username=username,
                role="group_leader",
                team_id=tid,
                password_hash=None if reset else hash_password(password),
                password_reset_required=reset,
            )
        return tid


async def test_login_step1_anti_enumeration_existing_and_missing(client):
    await _seed_login_user("realuser", "pw-secret-1")
    r_exist = await client.post("/login", data={"username": "realuser"})
    r_missing = await client.post("/login", data={"username": "ghostuser"})
    assert r_exist.status_code == 303
    assert r_missing.status_code == 303
    assert r_exist.headers["location"] == "/login/password"
    assert r_missing.headers["location"] == "/login/password"
    # sms_login cookie выставлен в обоих случаях.
    assert "sms_login" in r_exist.headers.get("set-cookie", "")
    assert "sms_login" in r_missing.headers.get("set-cookie", "")


async def test_login_password_success_sets_session(client):
    await _seed_login_user("loginok", "pw-secret-2")
    r = await client.post(
        "/login/password",
        data={"password": "pw-secret-2"},
        cookies={"sms_login": "loginok"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "sms_session" in r.headers.get("set-cookie", "")


async def test_login_password_invalid_401(client):
    await _seed_login_user("loginbad", "right-pw-1")
    r = await client.post(
        "/login/password",
        data={"password": "wrong-pw"},
        cookies={"sms_login": "loginbad"},
    )
    assert r.status_code == 401


async def test_login_lockout_after_threshold(client):
    await _seed_login_user("lockme", "correct-pw-9")
    settings = get_settings()
    last = None
    for _ in range(settings.LOGIN_FAILURE_THRESHOLD):
        last = await client.post(
            "/login/password",
            data={"password": "nope"},
            cookies={"sms_login": "lockme"},
        )
    assert last is not None and last.status_code == 401
    # Следующая попытка (даже с верным паролем) — заблокирована 423.
    r = await client.post(
        "/login/password",
        data={"password": "correct-pw-9"},
        cookies={"sms_login": "lockme"},
    )
    assert r.status_code == 423
    async with make_session() as s:
        lock = (
            await s.execute(
                text("SELECT lockout_until FROM users WHERE username='lockme'")
            )
        ).scalar()
    assert lock is not None


async def test_set_password_required_redirects_then_sets(client):
    await _seed_login_user("newbie", "unused", reset=True)
    r1 = await client.post(
        "/login/password",
        data={"password": "anything"},
        cookies={"sms_login": "newbie"},
    )
    assert r1.status_code == 303
    assert r1.headers["location"] == "/set-password"
    setcookie = r1.headers.get("set-cookie", "")
    assert "sms_setup" in setcookie
    # Извлечь setup-токен из cookie.
    import re

    m = re.search(r"sms_setup=([^;]+)", setcookie)
    assert m
    setup_token = m.group(1)
    # /set-password защищён CSRF (setup-сессия): нужен csrf_token из сессии.
    from app.infrastructure.sessions import SetupSessionStore

    setup = await SetupSessionStore().get(setup_token)
    assert setup is not None
    r2 = await client.post(
        "/set-password",
        data={
            "password": "brand-new-pw",
            "password_confirm": "brand-new-pw",
            "csrf_token": setup.csrf_token,
        },
        cookies={"sms_setup": setup_token},
    )
    assert r2.status_code == 302
    assert "sms_session" in r2.headers.get("set-cookie", "")
    async with make_session() as s:
        flag = (
            await s.execute(
                text(
                    "SELECT password_reset_required FROM users WHERE username='newbie'"
                )
            )
        ).scalar()
    assert flag is False
