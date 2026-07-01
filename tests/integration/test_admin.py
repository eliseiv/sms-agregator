"""Integration: admin users/teams API + guards (docs/06 §Auth 12,13,15; §5)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from shared.db import make_session
from tests.conftest import make_auth, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _seed_super_admin(username: str = "root") -> int:
    async with make_session() as s:
        async with s.begin():
            u = await seed_user(s, username=username, role="super_admin", team_id=None)
        return u.id


async def _count(sql: str, **p) -> int:
    async with make_session() as s:
        return int((await s.execute(text(sql), p)).scalar() or 0)


# --- create_user ------------------------------------------------------------


async def test_create_user_requires_team(client):
    admin_id = await _seed_super_admin()
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/users",
        json={"username": "noteam"},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "team_required"


async def test_create_first_user_becomes_leader(client):
    admin_id = await _seed_super_admin()
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "empty-team")
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/users",
        json={"username": "firstone", "team_id": tid},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "group_leader"
    # password_hash NULL + reset_required True.
    async with make_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT password_hash, password_reset_required "
                    "FROM users WHERE username='firstone'"
                )
            )
        ).one()
    assert row[0] is None
    assert row[1] is True


async def test_create_second_user_is_member(client):
    admin_id = await _seed_super_admin()
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "t2")
            await seed_user(s, username="lead2", role="group_leader", team_id=tid)
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/users",
        json={"username": "member2", "team_id": tid},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 201
    assert r.json()["role"] == "group_member"


# --- guards -----------------------------------------------------------------


async def test_group_member_forbidden_on_admin(client):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "guard-team")
            await seed_user(s, username="gl", role="group_leader", team_id=tid)
            member = await seed_user(s, username="gm", role="group_member", team_id=tid)
    cookies, headers = await make_auth(member.id, "group_member", tid)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/users",
        json={"username": "x", "team_id": tid},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 403


# --- delete guards ----------------------------------------------------------


async def test_cannot_delete_super_admin(client):
    admin_id = await _seed_super_admin()
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    r = await client.request(
        "DELETE", f"/api/admin/users/{admin_id}", cookies=cookies, headers=headers
    )
    assert r.status_code == 403
    assert r.json()["error"] == "cannot_delete_super_admin"


async def test_delete_leader_of_nonempty_team_conflict(client):
    admin_id = await _seed_super_admin()
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "busy-team")
            lead = await seed_user(s, username="bl", role="group_leader", team_id=tid)
            await seed_user(s, username="bm", role="group_member", team_id=tid)
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    r = await client.request(
        "DELETE", f"/api/admin/users/{lead.id}", cookies=cookies, headers=headers
    )
    assert r.status_code == 409
    assert r.json()["error"] == "user_is_leader"


async def test_delete_solo_leader_nulls_team_leader(client):
    admin_id = await _seed_super_admin()
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "solo-team")
            lead = await seed_user(s, username="sl", role="group_leader", team_id=tid)
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    r = await client.request(
        "DELETE", f"/api/admin/users/{lead.id}", cookies=cookies, headers=headers
    )
    assert r.status_code == 200
    async with make_session() as s:
        leader = (
            await s.execute(
                text("SELECT leader_user_id FROM teams WHERE id=:t"), {"t": tid}
            )
        ).scalar()
    assert leader is None


# --- teams CRUD -------------------------------------------------------------


async def test_teams_create_rename_and_delete_empty(client):
    admin_id = await _seed_super_admin()
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/teams", json={"name": "Alpha"}, cookies=cookies, headers=headers
    )
    assert r.status_code == 201
    team_id = r.json()["id"]
    r = await client.patch(
        f"/api/admin/teams/{team_id}",
        json={"name": "Beta"},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Beta"
    r = await client.request(
        "DELETE", f"/api/admin/teams/{team_id}", cookies=cookies, headers=headers
    )
    assert r.status_code == 200


async def test_team_delete_with_members_conflict(client):
    admin_id = await _seed_super_admin()
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "full-team")
            await seed_user(s, username="fl", role="group_leader", team_id=tid)
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    r = await client.request(
        "DELETE", f"/api/admin/teams/{tid}", cookies=cookies, headers=headers
    )
    assert r.status_code == 409
    assert r.json()["error"] == "team_has_members"


async def test_set_leader_user_not_in_team(client):
    admin_id = await _seed_super_admin()
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "team-A")
            t2 = await seed_team(s, "team-B")
            await seed_user(s, username="la", role="group_leader", team_id=t1)
            outsider = await seed_user(
                s, username="lb", role="group_leader", team_id=t2
            )
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/admin/teams/{t1}/leader",
        json={"new_leader_user_id": outsider.id},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "user_not_in_team"


# --- reset password ---------------------------------------------------------


async def test_cannot_reset_super_admin(client):
    admin_id = await _seed_super_admin("resetsa")
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    r = await client.post(
        f"/api/admin/users/{admin_id}/reset", cookies=cookies, headers=headers
    )
    assert r.status_code == 403
    assert r.json()["error"] == "cannot_reset_super_admin"


async def test_reset_password_revokes_links(client):
    admin_id = await _seed_super_admin()
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "rst-team")
            u = await seed_user(s, username="rstu", role="group_leader", team_id=tid)
            from app.infrastructure.repositories import TelegramLinkRepository

            await TelegramLinkRepository(s).upsert(telegram_user_id=770, user_id=u.id)
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    r = await client.post(
        f"/api/admin/users/{u.id}/reset", cookies=cookies, headers=headers
    )
    assert r.status_code == 200
    assert (
        await _count("SELECT count(*) FROM telegram_links WHERE user_id=:u", u=u.id)
        == 0
    )
    assert (
        await _count(
            "SELECT count(*) FROM users WHERE id=:u AND password_reset_required",
            u=u.id,
        )
        == 1
    )
