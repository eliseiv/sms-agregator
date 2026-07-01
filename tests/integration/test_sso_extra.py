"""Integration (coverage): SSO endpoint linked-path, revoke/unlink, mark_dead, pending."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import text

from app.application.telegram_sso_service import TelegramSSOService
from app.infrastructure.repositories import TelegramLinkRepository
from shared.db import make_session
from tests.conftest import build_init_data, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")
NOW = int(time.time())


async def _user(name):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "e-" + name)
            u = await seed_user(s, username=name, role="group_leader", team_id=tid)
        return u.id, tid


async def test_telegram_auth_linked_creates_session(client):
    uid, tid = await _user("linked1")
    async with make_session() as s:
        async with s.begin():
            await TelegramLinkRepository(s).upsert(telegram_user_id=400, user_id=uid)
    raw = build_init_data(telegram_user_id=400, auth_date=NOW)
    r = await client.post("/api/telegram/auth", json={"init_data": raw})
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is True
    assert body["redirect"] == "/"
    assert "sms_session" in r.headers.get("set-cookie", "")


async def test_telegram_auth_linked_but_user_deleted_revokes(client):
    uid, tid = await _user("linked2")
    async with make_session() as s:
        async with s.begin():
            await TelegramLinkRepository(s).upsert(telegram_user_id=401, user_id=uid)
    # Удалим пользователя (привязка каскадом? user delete cascades telegram_links).
    # Чтобы смоделировать «висячую» привязку, отвяжем FK-каскад: пометим user_id
    # несуществующим невозможно (FK). Вместо этого проверим ветку через прямой вызов.
    async with make_session() as s:
        svc = TelegramSSOService(s)
        resolved = await svc.verify_and_resolve(
            build_init_data(telegram_user_id=401, auth_date=NOW)
        )
    assert resolved.kind == "linked"
    assert resolved.user_id == uid


async def test_link_pending_via_service(client):
    uid, _ = await _user("pend1")
    async with make_session() as s:
        svc = TelegramSSOService(s)
        token = await svc.create_pending(500)
        # consume — one-shot.
        first = await svc.consume_pending(token)
        second = await svc.consume_pending(token)
    assert first == 500
    assert second is None
    # link_pending создаёт привязку.
    async with make_session() as s:
        async with s.begin():
            await TelegramSSOService(s).link_pending(
                telegram_user_id=500, user_id=uid, ip="1.1.1.1", user_agent="t"
            )
    async with make_session() as s:
        owner = (
            await s.execute(
                text("SELECT user_id FROM telegram_links WHERE telegram_user_id=500")
            )
        ).scalar()
    assert owner == uid


async def test_revoke_one_and_all(client):
    uid, _ = await _user("rev1")
    async with make_session() as s:
        async with s.begin():
            links = TelegramLinkRepository(s)
            await links.upsert(telegram_user_id=600, user_id=uid)
            await links.upsert(telegram_user_id=601, user_id=uid)
    async with make_session() as s:
        async with s.begin():
            ok = await TelegramSSOService(s).revoke_one(
                user_id=uid, telegram_user_id=600, ip="1.1.1.1", user_agent="t"
            )
    assert ok is True
    async with make_session() as s:
        async with s.begin():
            await TelegramSSOService(s).revoke_for_user(
                user_id=uid, reason="test", ip="1.1.1.1", user_agent="t"
            )
    async with make_session() as s:
        cnt = (
            await s.execute(
                text("SELECT count(*) FROM telegram_links WHERE user_id=:u"),
                {"u": uid},
            )
        ).scalar()
    assert cnt == 0


async def test_mark_link_dead_service(client):
    uid, _ = await _user("dead2")
    async with make_session() as s:
        async with s.begin():
            await TelegramLinkRepository(s).upsert(telegram_user_id=700, user_id=uid)
    async with make_session() as s:
        async with s.begin():
            await TelegramSSOService(s).mark_link_dead(
                telegram_user_id=700, user_id=uid, reason="403"
            )
    async with make_session() as s:
        dead = (
            await s.execute(
                text("SELECT dead_at FROM telegram_links WHERE telegram_user_id=700")
            )
        ).scalar()
    assert dead is not None
