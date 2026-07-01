"""Integration (coverage): numbers list/delete, invalid phone, team_not_found."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from shared.db import make_session
from tests.conftest import make_auth, seed_number, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _team_member(name):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, name)
            await seed_user(s, username=name + "l", role="group_leader", team_id=tid)
            m = await seed_user(s, username=name + "m", role="group_member", team_id=tid)
        return tid, m.id


async def test_list_numbers_member_scoped(client):
    tid, mid = await _team_member("nx1")
    async with make_session() as s:
        async with s.begin():
            await seed_number(s, phone="+441234561111", team_id=tid)
    cookies, headers = await make_auth(mid, "group_member", tid)
    r = await client.get("/api/numbers", cookies=cookies, headers=headers)
    assert r.status_code == 200
    nums = r.json()["numbers"]
    assert len(nums) == 1
    assert nums[0]["team_id"] == tid


async def test_list_numbers_superadmin_all_and_by_team(client):
    tid, _ = await _team_member("nx2")
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(s, username="nx2root", role="super_admin", team_id=None)
            await seed_number(s, phone="+441234562222", team_id=tid)
    cookies, headers = await make_auth(admin.id, "super_admin", None)
    r_all = await client.get("/api/numbers", cookies=cookies, headers=headers)
    assert r_all.status_code == 200
    assert len(r_all.json()["numbers"]) >= 1
    r_team = await client.get(
        f"/api/numbers?team_id={tid}", cookies=cookies, headers=headers
    )
    assert r_team.status_code == 200
    assert all(n["team_id"] == tid for n in r_team.json()["numbers"])


async def test_delete_own_number(client):
    tid, mid = await _team_member("nx3")
    async with make_session() as s:
        async with s.begin():
            num = await seed_number(s, phone="+441234563333", team_id=tid)
    cookies, headers = await make_auth(mid, "group_member", tid)
    r = await client.request(
        "DELETE", f"/api/numbers/{num.id}", cookies=cookies, headers=headers
    )
    assert r.status_code == 200
    async with make_session() as s:
        cnt = (
            await s.execute(
                text("SELECT count(*) FROM phone_numbers WHERE id=:i"), {"i": num.id}
            )
        ).scalar()
    assert cnt == 0


async def test_delete_foreign_number_forbidden(client):
    tid_a, mid_a = await _team_member("nx4a")
    tid_b, _ = await _team_member("nx4b")
    async with make_session() as s:
        async with s.begin():
            num = await seed_number(s, phone="+441234564444", team_id=tid_b)
    cookies, headers = await make_auth(mid_a, "group_member", tid_a)
    r = await client.request(
        "DELETE", f"/api/numbers/{num.id}", cookies=cookies, headers=headers
    )
    assert r.status_code == 403


async def test_delete_missing_number_404(client):
    tid, mid = await _team_member("nx5")
    cookies, headers = await make_auth(mid, "group_member", tid)
    r = await client.request(
        "DELETE", "/api/numbers/999999", cookies=cookies, headers=headers
    )
    assert r.status_code == 404


async def test_invalid_phone_number_400(client):
    tid, mid = await _team_member("nx6")
    cookies, headers = await make_auth(mid, "group_member", tid)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/numbers",
        json={"phone_number": "not-a-number"},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_phone_number"


async def test_superadmin_team_not_found_404(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(s, username="nx7root", role="super_admin", team_id=None)
    cookies, headers = await make_auth(admin.id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/numbers",
        json={"phone_number": "+441234567777", "team_id": 987654},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 404
    assert r.json()["error"] == "team_not_found"
