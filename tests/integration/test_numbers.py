"""Integration: /api/numbers — доступ по команде, дубликаты (docs/06 §15; §6)."""

from __future__ import annotations

import pytest

from shared.db import make_session
from tests.conftest import make_auth, seed_number, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _team_with_member(name: str):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, name)
            lead = await seed_user(
                s, username=name + "-l", role="group_leader", team_id=tid
            )
            member = await seed_user(
                s, username=name + "-m", role="group_member", team_id=tid
            )
        return tid, lead.id, member.id


async def test_member_adds_number_to_own_team(client):
    tid, _, member_id = await _team_with_member("num1")
    cookies, headers = await make_auth(member_id, "group_member", tid)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/numbers",
        json={"phone_number": "+441234511111"},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["team_id"] == tid


async def test_duplicate_number_conflict(client):
    tid, _, member_id = await _team_with_member("num2")
    async with make_session() as s:
        async with s.begin():
            await seed_number(s, phone="+441234522222", team_id=tid)
    cookies, headers = await make_auth(member_id, "group_member", tid)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/numbers",
        json={"phone_number": "+441234522222"},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 409
    assert r.json()["error"] == "phone_number_taken"


async def test_member_cannot_add_to_foreign_team(client):
    tid_a, _, member_a = await _team_with_member("num3a")
    tid_b, _, _ = await _team_with_member("num3b")
    cookies, headers = await make_auth(member_a, "group_member", tid_a)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/numbers",
        json={"phone_number": "+441234533333", "team_id": tid_b},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 403


async def test_super_admin_must_pass_team_id(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="numroot", role="super_admin", team_id=None
            )
            tid = await seed_team(s, "num4")
            await seed_user(s, username="num4-l", role="group_leader", team_id=tid)
    cookies, headers = await make_auth(admin.id, "super_admin", None)
    headers["content-type"] = "application/json"
    # Без team_id → 400.
    r0 = await client.post(
        "/api/numbers",
        json={"phone_number": "+441234544444"},
        cookies=cookies,
        headers=headers,
    )
    assert r0.status_code == 400
    assert r0.json()["error"] == "team_required"
    # С явным team_id → 201.
    r1 = await client.post(
        "/api/numbers",
        json={"phone_number": "+441234544444", "team_id": tid},
        cookies=cookies,
        headers=headers,
    )
    assert r1.status_code == 201
    assert r1.json()["team_id"] == tid
