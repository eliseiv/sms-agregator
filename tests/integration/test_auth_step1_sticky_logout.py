"""Integration: шаг-1 ветвление логина + sticky logout (docs/06 §12, §14, §16a; ADR-0011)."""

from __future__ import annotations

import re
import time

import pytest
from sqlalchemy import text

from app.core.security import hash_password
from app.infrastructure.sessions import SetupSessionStore
from shared.db import make_session
from tests.conftest import build_init_data, make_auth, seed_link, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")
NOW = int(time.time())


async def _seed(username, *, password=None, reset=False, tg_id=None):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "t-" + username)
            u = await seed_user(
                s,
                username=username,
                role="group_leader",
                team_id=tid,
                password_hash=hash_password(password) if password else None,
                password_reset_required=reset,
            )
            if tg_id is not None:
                await seed_link(s, telegram_user_id=tg_id, user_id=u.id)
        return u.id, tid


# --- №12: первый вход -------------------------------------------------------


async def test_first_login_goes_to_set_password_not_password_form(client):
    """Аккаунт без пароля (reset) → шаг-1 сразу /set-password, sms_login НЕ ставится."""
    await _seed("newbie12", reset=True)
    r = await client.post("/login", data={"username": "newbie12"})
    assert r.status_code == 303
    assert r.headers["location"] == "/set-password"
    setcookie = r.headers.get("set-cookie", "")
    assert "sms_setup=" in setcookie  # setup-сессия выдана
    assert "sms_login=" not in setcookie  # форма ввода пароля НЕ используется


async def test_first_login_completes_to_landing_200(client):
    await _seed("newbie12b", reset=True)
    r1 = await client.post("/login", data={"username": "newbie12b"})
    assert r1.headers["location"] == "/set-password"
    token = re.search(r"sms_setup=([^;]+)", r1.headers.get("set-cookie", "")).group(1)
    setup = await SetupSessionStore().get(token)
    r2 = await client.post(
        "/set-password",
        data={
            "password": "brand-new-pw-1",
            "password_confirm": "brand-new-pw-1",
            "csrf_token": setup.csrf_token,
        },
        follow_redirects=True,
    )
    assert r2.status_code == 200
    assert str(r2.url).endswith("/app")


async def test_active_user_login_goes_to_password_form(client):
    await _seed("active12", password="active-pw-123")
    r = await client.post("/login", data={"username": "active12"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login/password"
    assert "sms_login=" in r.headers.get("set-cookie", "")
    # шаг-2 доводит до входа
    r2 = await client.post(
        "/login/password",
        data={"password": "active-pw-123"},
        cookies={"sms_login": "active12"},
        follow_redirects=True,
    )
    assert r2.status_code == 200
    assert str(r2.url).endswith("/app")


# --- №14: анти-энумерация ---------------------------------------------------


async def test_anti_enumeration_active_and_missing_indistinguishable(client):
    await _seed("realuser14", password="pw-14-secret")
    r_active = await client.post("/login", data={"username": "realuser14"})
    r_missing = await client.post("/login", data={"username": "ghost14"})
    # Оба → /login/password, оба ставят sms_login (неотличимы).
    assert r_active.status_code == r_missing.status_code == 303
    assert r_active.headers["location"] == "/login/password"
    assert r_missing.headers["location"] == "/login/password"
    assert "sms_login=" in r_active.headers.get("set-cookie", "")
    assert "sms_login=" in r_missing.headers.get("set-cookie", "")


async def test_first_login_state_is_distinguishable_td010(client):
    """Принятый TD-010: состояние 'первый вход' отличимо (→ /set-password)."""
    await _seed("firstlogin14", reset=True)
    r = await client.post("/login", data={"username": "firstlogin14"})
    assert r.headers["location"] == "/set-password"  # отличается от /login/password


# --- №16a: sticky logout ----------------------------------------------------


async def test_logout_sets_sticky_marker_and_suppresses_auto_sso(client):
    uid, tid = await _seed("sticky16", password="pw-16-secret", tg_id=16001)

    # 1) Логин через шаг-2 → сессия.
    r_login = await client.post(
        "/login/password",
        data={"password": "pw-16-secret"},
        cookies={"sms_login": "sticky16"},
    )
    assert r_login.status_code == 303
    assert "sms_session=" in r_login.headers.get("set-cookie", "")

    # 2) Logout → маркер sms_logged_out + чистит сессию.
    cookies, headers = await make_auth(uid, "group_leader", tid)
    r_logout = await client.post("/logout", cookies=cookies, headers=headers)
    assert r_logout.status_code == 302
    logout_sc = r_logout.headers.get("set-cookie", "")
    assert "sms_logged_out=1" in logout_sc

    # 3) Авто-SSO с маркером и БЕЗ сессии (живая привязка!) → НЕ логинит обратно.
    raw = build_init_data(telegram_user_id=16001, auth_date=NOW)
    r_sso = await client.post(
        "/api/telegram/auth",
        json={"init_data": raw},
        cookies={"sms_logged_out": "1"},
    )
    assert r_sso.status_code == 200
    body = r_sso.json()
    assert body["linked"] is False
    assert body["logged_out"] is True
    # НЕТ Set-Cookie сессии — не перелогинивает.
    assert "sms_session=" not in r_sso.headers.get("set-cookie", "")

    # telegram_links переживают logout.
    async with make_session() as s:
        cnt = (
            await s.execute(
                text(
                    "SELECT count(*) FROM telegram_links "
                    "WHERE telegram_user_id=16001 AND dead_at IS NULL"
                )
            )
        ).scalar()
    assert cnt == 1


async def test_explicit_login_clears_marker_then_sso_logs_in(client):
    uid, tid = await _seed("sticky16b", password="pw-16b-secret", tg_id=16002)

    # Явный вход (session_created) чистит маркер (Max-Age=0).
    r_login = await client.post(
        "/login/password",
        data={"password": "pw-16b-secret"},
        cookies={"sms_login": "sticky16b", "sms_logged_out": "1"},
    )
    assert r_login.status_code == 303
    sc = r_login.headers.get("set-cookie", "")
    assert "sms_logged_out=" in sc
    assert "max-age=0" in sc.lower() or "expires=" in sc.lower()

    # После снятия маркера авто-SSO на устройстве БЕЗ сессии и БЕЗ маркера
    # (эмулируем чистый контекст) → создаёт сессию (linked).
    client.cookies.clear()
    raw = build_init_data(telegram_user_id=16002, auth_date=NOW)
    r_sso = await client.post("/api/telegram/auth", json={"init_data": raw})
    assert r_sso.status_code == 200
    assert r_sso.json()["linked"] is True
    assert "sms_session=" in r_sso.headers.get("set-cookie", "")


async def test_self_heal_clears_stale_marker(client):
    uid, tid = await _seed("sticky16c", password="pw-16c", tg_id=16003)
    cookies, _ = await make_auth(uid, "group_leader", tid)
    # Активная сессия + stale-маркер → self-heal и очистка маркера.
    raw = build_init_data(telegram_user_id=16003, auth_date=NOW)
    r = await client.post(
        "/api/telegram/auth",
        json={"init_data": raw},
        cookies={**cookies, "sms_logged_out": "1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is False
    assert body.get("healed") is True
    sc = r.headers.get("set-cookie", "")
    assert "sms_logged_out=" in sc  # маркер очищается
    assert "max-age=0" in sc.lower() or "expires=" in sc.lower()
