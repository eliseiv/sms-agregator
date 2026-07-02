"""Integration: тест 11a — инвариант достижимости landing (docs/06 §11a, ADR-0008).

Следуем по редиректам и утверждаем ТЕРМИНАЛЬНЫЙ рендер 200 для каждой роли
(не только промежуточный 30x). Пост-логин и пост-set-password не приводят к 404.
"""

from __future__ import annotations

import re

import pytest

from app.core.security import hash_password
from app.infrastructure.sessions import SetupSessionStore
from shared.db import make_session
from tests.conftest import make_auth, seed_link, seed_number, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _set_auth(client, user_id, role, team_id):
    cookies, _ = await make_auth(user_id, role, team_id)
    client.cookies.set("sms_session", cookies["sms_session"])


# --- GET / диспетчер --------------------------------------------------------


async def test_root_no_session_redirects_login(client):
    r = await client.get("/")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


async def test_root_super_admin_followed_renders_admin_200(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="ld-root", role="super_admin", team_id=None
            )
    await _set_auth(client, admin.id, "super_admin", None)
    # промежуточный редирект
    r_hop = await client.get("/")
    assert r_hop.status_code == 302
    assert r_hop.headers["location"] == "/admin"
    # follow-through → терминальный 200
    r = await client.get("/", follow_redirects=True)
    assert r.status_code == 200
    assert str(r.url).endswith("/admin")


@pytest.mark.parametrize("role", ["group_leader", "group_member"])
async def test_root_team_role_followed_renders_app_200(client, role):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "ld-" + role)
            lead = await seed_user(
                s, username=role + "-lead", role="group_leader", team_id=tid
            )
            if role == "group_member":
                u = await seed_user(
                    s, username=role + "-mem", role="group_member", team_id=tid
                )
            else:
                u = lead
            await seed_number(s, phone="+441288800010", team_id=tid)
    await _set_auth(client, u.id, role, tid)
    r_hop = await client.get("/")
    assert r_hop.status_code == 302
    assert r_hop.headers["location"] == "/app"
    r = await client.get("/", follow_redirects=True)
    assert r.status_code == 200
    assert str(r.url).endswith("/app")
    body = r.text
    # содержит: имя команды, блок Telegram-статуса, список номеров, форму, logout.
    assert "ld-" + role in body  # имя команды
    assert "Уведомления в Telegram" in body
    assert "Номера команды" in body
    assert "+441288800010" in body
    assert "Добавить номер" in body
    assert 'action="/logout"' in body


# --- /app прямые guards -----------------------------------------------------


async def test_app_no_session_redirects_login(client):
    r = await client.get("/app")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


async def test_app_super_admin_redirects_admin_no_404(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="app-root", role="super_admin", team_id=None
            )
    await _set_auth(client, admin.id, "super_admin", None)
    r = await client.get("/app")
    assert r.status_code == 302
    assert r.headers["location"] == "/admin"
    # follow → терминальный 200 (нет 404-тупика)
    r2 = await client.get("/app", follow_redirects=True)
    assert r2.status_code == 200


# --- SSR-контекст: только свои номера + telegram-статус ----------------------


async def test_app_member_sees_only_own_team_numbers(client):
    async with make_session() as s:
        async with s.begin():
            ta = await seed_team(s, "own-A")
            tb = await seed_team(s, "other-B")
            await seed_user(s, username="ownA-l", role="group_leader", team_id=ta)
            mem = await seed_user(s, username="ownA-m", role="group_member", team_id=ta)
            await seed_user(s, username="otherB-l", role="group_leader", team_id=tb)
            await seed_number(s, phone="+441288801111", team_id=ta)
            await seed_number(s, phone="+441288802222", team_id=tb)
    await _set_auth(client, mem.id, "group_member", ta)
    r = await client.get("/app")
    assert r.status_code == 200
    body = r.text
    assert "+441288801111" in body  # свой номер
    assert "+441288802222" not in body  # чужой команды — не виден
    # has_telegram_link=false → "Не привязан"
    assert "Не привязан" in body


async def test_app_has_telegram_link_true_when_active(client):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "tg-A")
            await seed_user(s, username="tgA-l", role="group_leader", team_id=tid)
            mem = await seed_user(s, username="tgA-m", role="group_member", team_id=tid)
            await seed_link(s, telegram_user_id=90001, user_id=mem.id)
    await _set_auth(client, mem.id, "group_member", tid)
    r = await client.get("/app")
    assert r.status_code == 200
    assert "Привязан" in r.text
    assert "Не привязан" not in r.text


# --- Пост-логин: терминальный 200 (не 404) ----------------------------------


async def test_post_login_member_lands_200(client):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "pl-team")
            await seed_user(s, username="pl-lead", role="group_leader", team_id=tid)
            await seed_user(
                s,
                username="plmember",
                role="group_member",
                team_id=tid,
                password_hash=hash_password("member-pw-123"),
            )
    r1 = await client.post("/login", data={"username": "plmember"})
    assert r1.status_code == 303
    assert r1.headers["location"] == "/login/password"
    # cookie sms_login попал в jar → follow-through логина до терминала.
    r2 = await client.post(
        "/login/password",
        data={"password": "member-pw-123"},
        follow_redirects=True,
    )
    assert r2.status_code == 200
    assert str(r2.url).endswith("/app")
    assert "Номера команды" in r2.text


async def test_post_set_password_first_login_lands_200(client):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "sp-team")
            await seed_user(s, username="sp-lead", role="group_leader", team_id=tid)
            await seed_user(
                s,
                username="spmember",
                role="group_member",
                team_id=tid,
                password_reset_required=True,
            )
    # Амендмент ADR-0002: шаг-1 для аккаунта без пароля → сразу /set-password.
    r1 = await client.post("/login", data={"username": "spmember"})
    assert r1.status_code == 303
    assert r1.headers["location"] == "/set-password"
    setup_token = re.search(
        r"sms_setup=([^;]+)", r1.headers.get("set-cookie", "")
    ).group(1)
    setup = await SetupSessionStore().get(setup_token)
    r3 = await client.post(
        "/set-password",
        data={
            "password": "fresh-pw-9876",
            "password_confirm": "fresh-pw-9876",
            "csrf_token": setup.csrf_token,
        },
        follow_redirects=True,
    )
    assert r3.status_code == 200
    assert str(r3.url).endswith("/app")
    assert "Номера команды" in r3.text
