"""Integration: CSRF-защита и exempt-маршруты (docs/06 §CSRF; docs/08 §3)."""

from __future__ import annotations

import time

import pytest

from shared.db import make_session
from tests.conftest import build_init_data, make_auth, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_admin_post_without_csrf_is_403(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="csrfroot", role="super_admin", team_id=None
            )
    cookies, _headers = await make_auth(admin.id, "super_admin", None)
    # Отправляем БЕЗ X-CSRF-Token.
    r = await client.post(
        "/api/admin/teams",
        json={"name": "NoCsrf"},
        cookies=cookies,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "csrf_failed"


async def test_admin_post_with_csrf_passes(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="csrfroot2", role="super_admin", team_id=None
            )
    cookies, headers = await make_auth(admin.id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/teams",
        json={"name": "WithCsrf"},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 201


async def test_telegram_auth_is_csrf_exempt(client):
    raw = build_init_data(telegram_user_id=990, auth_date=int(time.time()))
    # Без cookie/csrf — не должно быть 403 csrf_failed.
    r = await client.post("/api/telegram/auth", json={"init_data": raw})
    assert r.status_code != 403
    assert r.status_code == 200


async def test_login_steps_are_csrf_exempt(client):
    r = await client.post("/login", data={"username": "someone"})
    assert r.status_code == 303  # не 403
