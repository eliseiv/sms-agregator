"""Integration (coverage): admin PATCH move-team, teams set_leader/rename/create/delete."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from shared.db import make_session
from tests.conftest import make_auth, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _super_admin(name="xroot"):
    async with make_session() as s:
        async with s.begin():
            u = await seed_user(s, username=name, role="super_admin", team_id=None)
        return u.id


async def _val(sql, **p):
    async with make_session() as s:
        return (await s.execute(text(sql), p)).scalar()


# --- list endpoints ---------------------------------------------------------


async def test_list_users_and_teams(client):
    admin_id = await _super_admin("lst")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "list-team")
            await seed_user(s, username="lu", role="group_leader", team_id=tid)
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    r1 = await client.get("/api/admin/users", cookies=cookies, headers=headers)
    assert r1.status_code == 200
    assert any(u["username"] == "lu" for u in r1.json()["users"])
    r2 = await client.get("/api/admin/teams", cookies=cookies, headers=headers)
    assert r2.status_code == 200
    assert any(t["name"] == "list-team" for t in r2.json()["teams"])


# --- set_leader full swap ---------------------------------------------------


async def test_set_leader_swaps_roles(client):
    admin_id = await _super_admin("swap")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "swap-team")
            l1 = await seed_user(s, username="l1", role="group_leader", team_id=tid)
            m = await seed_user(s, username="m1", role="group_member", team_id=tid)
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/admin/teams/{tid}/leader",
        json={"new_leader_user_id": m.id},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["leader_user_id"] == m.id
    assert await _val("SELECT role FROM users WHERE id=:i", i=m.id) == "group_leader"
    assert await _val("SELECT role FROM users WHERE id=:i", i=l1.id) == "group_member"


# --- move-team paths --------------------------------------------------------


async def test_move_member_to_other_team(client):
    admin_id = await _super_admin("mv")
    async with make_session() as s:
        async with s.begin():
            a = await seed_team(s, "mv-a")
            b = await seed_team(s, "mv-b")
            await seed_user(s, username="mv-al", role="group_leader", team_id=a)
            mem = await seed_user(s, username="mv-am", role="group_member", team_id=a)
            await seed_user(s, username="mv-bl", role="group_leader", team_id=b)
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/admin/users/{mem.id}",
        json={"team_id": b},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["team_id"] == b
    assert r.json()["role"] == "group_member"


async def test_move_leader_of_nonempty_team_forbidden(client):
    admin_id = await _super_admin("mvf")
    async with make_session() as s:
        async with s.begin():
            a = await seed_team(s, "mvf-a")
            b = await seed_team(s, "mvf-b")
            lead = await seed_user(s, username="mvf-l", role="group_leader", team_id=a)
            await seed_user(s, username="mvf-m", role="group_member", team_id=a)
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/admin/users/{lead.id}",
        json={"team_id": b},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 409
    assert r.json()["error"] == "leader_move_forbidden"


async def test_move_solo_leader_becomes_leader_of_target(client):
    admin_id = await _super_admin("mvs")
    async with make_session() as s:
        async with s.begin():
            a = await seed_team(s, "mvs-a")
            b = await seed_team(s, "mvs-b")  # пустая
            lead = await seed_user(s, username="mvs-l", role="group_leader", team_id=a)
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/admin/users/{lead.id}",
        json={"team_id": b},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["team_id"] == b
    assert r.json()["role"] == "group_leader"
    # старая команда осталась без лидера
    assert await _val("SELECT leader_user_id FROM teams WHERE id=:i", i=a) is None


async def test_move_super_admin_rejected(client):
    admin_id = await _super_admin("mvsa")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "mvsa-t")
            await seed_user(s, username="mvsa-l", role="group_leader", team_id=tid)
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/admin/users/{admin_id}",
        json={"team_id": tid},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "role_team_invariant"


async def test_update_display_name_only(client):
    admin_id = await _super_admin("dn")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "dn-t")
            u = await seed_user(s, username="dn-u", role="group_leader", team_id=tid)
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/admin/users/{u.id}",
        json={"display_name": "Имя Пользователя"},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["display_name"] == "Имя Пользователя"


# --- teams create/rename conflicts ------------------------------------------


async def test_team_create_duplicate_name_conflict(client):
    admin_id = await _super_admin("tc")
    async with make_session() as s:
        async with s.begin():
            await seed_team(s, "Dup")
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/teams", json={"name": "Dup"}, cookies=cookies, headers=headers
    )
    assert r.status_code == 409
    assert r.json()["error"] == "team_name_taken"


async def test_team_rename_to_existing_conflict(client):
    admin_id = await _super_admin("tr")
    async with make_session() as s:
        async with s.begin():
            await seed_team(s, "Keep")
            t2 = await seed_team(s, "Change")
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/admin/teams/{t2}",
        json={"name": "Keep"},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 409
