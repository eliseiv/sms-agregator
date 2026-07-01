"""Integration: Mini App SSO endpoint + SSO decision-table (docs/06 §17-19)."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import text

from app.application.telegram_sso_service import TelegramSSOService
from app.exceptions import TelegramLinkLimitError
from shared.db import make_session
from tests.conftest import build_init_data, make_auth, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = int(time.time())


async def _seed_user_in_team(username: str):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "sso-" + username)
            u = await seed_user(s, username=username, role="group_leader", team_id=tid)
        return u.id, tid


# --- Endpoint ---------------------------------------------------------------


async def test_sso_unlinked_sets_pending_cookie(client):
    raw = build_init_data(telegram_user_id=111, auth_date=NOW)
    r = await client.post("/api/telegram/auth", json={"init_data": raw})
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is False
    assert "sms_tg_pending" in r.headers.get("set-cookie", "")


async def test_sso_expired_returns_401(client):
    raw = build_init_data(telegram_user_id=112, auth_date=NOW - 10_000)
    r = await client.post("/api/telegram/auth", json={"init_data": raw})
    assert r.status_code == 401
    assert r.json()["error"] == "init_data_expired"


async def test_sso_bad_hash_returns_401(client):
    raw = build_init_data(telegram_user_id=113, auth_date=NOW, valid_hash=False)
    r = await client.post("/api/telegram/auth", json={"init_data": raw})
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_init_data"


async def test_sso_self_heal_with_active_session(client):
    uid, tid = await _seed_user_in_team("healme")
    cookies, _ = await make_auth(uid, "group_leader", tid)
    raw = build_init_data(telegram_user_id=114, auth_date=NOW)
    r = await client.post(
        "/api/telegram/auth", json={"init_data": raw}, cookies=cookies
    )
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is False
    assert body["healed"] is True
    async with make_session() as s:
        cnt = (
            await s.execute(
                text(
                    "SELECT count(*) FROM telegram_links "
                    "WHERE telegram_user_id=114 AND user_id=:u AND dead_at IS NULL"
                ),
                {"u": uid},
            )
        ).scalar()
    assert cnt == 1


async def test_sso_flood_returns_429(client):
    raw = build_init_data(telegram_user_id=115, auth_date=NOW)
    statuses = []
    for _ in range(35):
        r = await client.post(
            "/api/telegram/auth",
            json={"init_data": raw},
            headers={"X-Forwarded-For": "203.0.113.7"},
        )
        statuses.append(r.status_code)
    assert 429 in statuses, f"ожидался 429 при флуде, got {set(statuses)}"


# --- SSO decision-table (service level) -------------------------------------


async def _audit_count(action: str, **p) -> int:
    async with make_session() as s:
        return int(
            (
                await s.execute(
                    text("SELECT count(*) FROM admin_audit WHERE action=:a"),
                    {"a": action},
                )
            ).scalar()
            or 0
        )


async def test_selfheal_live_link_is_noop(client):
    uid, _ = await _seed_user_in_team("noop")
    async with make_session() as s:
        async with s.begin():
            from app.infrastructure.repositories import TelegramLinkRepository

            await TelegramLinkRepository(s).upsert(telegram_user_id=200, user_id=uid)
        created = (
            await s.execute(
                text("SELECT created_at FROM telegram_links WHERE telegram_user_id=200")
            )
        ).scalar()
    before_audit = await _audit_count("telegram_link_created")
    async with make_session() as s:
        healed = await TelegramSSOService(s).self_heal_link(
            telegram_user_id=200, user_id=uid, ip="1.1.1.1", user_agent="t"
        )
    assert healed is True
    async with make_session() as s:
        created2 = (
            await s.execute(
                text("SELECT created_at FROM telegram_links WHERE telegram_user_id=200")
            )
        ).scalar()
    assert created2 == created  # NO-OP: created_at не изменился
    assert await _audit_count("telegram_link_created") == before_audit  # без аудита


async def test_selfheal_dead_link_reactivates(client):
    uid, _ = await _seed_user_in_team("dead")
    async with make_session() as s:
        async with s.begin():
            from app.infrastructure.repositories import TelegramLinkRepository

            links = TelegramLinkRepository(s)
            await links.upsert(telegram_user_id=201, user_id=uid)
            await links.mark_dead(201)
    async with make_session() as s:
        await TelegramSSOService(s).self_heal_link(
            telegram_user_id=201, user_id=uid, ip="1.1.1.1", user_agent="t"
        )
    async with make_session() as s:
        dead_at = (
            await s.execute(
                text("SELECT dead_at FROM telegram_links WHERE telegram_user_id=201")
            )
        ).scalar()
    assert dead_at is None  # реактивирована


async def test_selfheal_other_owner_rebinds(client):
    uid_a, _ = await _seed_user_in_team("owner-a")
    uid_b, _ = await _seed_user_in_team("owner-b")
    async with make_session() as s:
        async with s.begin():
            from app.infrastructure.repositories import TelegramLinkRepository

            await TelegramLinkRepository(s).upsert(telegram_user_id=202, user_id=uid_a)
    async with make_session() as s:
        await TelegramSSOService(s).self_heal_link(
            telegram_user_id=202, user_id=uid_b, ip="1.1.1.1", user_agent="t"
        )
    async with make_session() as s:
        owner = (
            await s.execute(
                text("SELECT user_id FROM telegram_links WHERE telegram_user_id=202")
            )
        ).scalar()
    assert owner == uid_b  # rebind на нового владельца
    assert await _audit_count("telegram_link_rebound") >= 1


async def test_link_session_add_respects_limit(client):
    uid, _ = await _seed_user_in_team("limited")
    # TG_MAX_LINKS_PER_USER=3 (env). Создаём 3 активные привязки.
    async with make_session() as s:
        async with s.begin():
            from app.infrastructure.repositories import TelegramLinkRepository

            links = TelegramLinkRepository(s)
            for tg in (301, 302, 303):
                await links.upsert(telegram_user_id=tg, user_id=uid)
    # 4-я через session_add (rebind запрещён) → лимит.
    with pytest.raises(TelegramLinkLimitError):
        async with make_session() as s:
            async with s.begin():
                await TelegramSSOService(s).link_session_add(
                    telegram_user_id=304, user_id=uid, ip="1.1.1.1", user_agent="t"
                )
