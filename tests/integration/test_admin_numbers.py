"""Integration: GET/PATCH /api/admin/numbers — unassigned-пул и распределение (docs/06, §4a)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from shared.db import make_session
from tests.conftest import make_auth, seed_number, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _super_admin(name):
    async with make_session() as s:
        async with s.begin():
            u = await seed_user(s, username=name, role="super_admin", team_id=None)
        return u.id


async def _val(sql, **p):
    async with make_session() as s:
        return (await s.execute(text(sql), p)).scalar()


# --- GET list ---------------------------------------------------------------


async def test_list_assignment_filters(client):
    admin = await _super_admin("an-root")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "an-team")
            await seed_user(s, username="an-l", role="group_leader", team_id=tid)
            await seed_number(s, phone="+441240000001", team_id=tid)  # assigned
            # unassigned: вставим напрямую (team_id NULL).
            await s.execute(
                text(
                    "INSERT INTO phone_numbers (phone_number, team_id, is_active) "
                    "VALUES ('+441240000002', NULL, true)"
                )
            )
    cookies, headers = await make_auth(admin, "super_admin", None)
    r_all = await client.get(
        "/api/admin/numbers?assignment=all", cookies=cookies, headers=headers
    )
    assert r_all.status_code == 200
    assert len(r_all.json()["numbers"]) == 2

    r_assigned = await client.get(
        "/api/admin/numbers?assignment=assigned", cookies=cookies, headers=headers
    )
    assert [n["phone_number"] for n in r_assigned.json()["numbers"]] == [
        "+441240000001"
    ]

    r_unassigned = await client.get(
        "/api/admin/numbers?assignment=unassigned", cookies=cookies, headers=headers
    )
    unassigned = r_unassigned.json()["numbers"]
    assert [n["phone_number"] for n in unassigned] == ["+441240000002"]
    assert unassigned[0]["team_name"] is None  # team_name=null для unassigned


async def test_list_invalid_assignment_400(client):
    admin = await _super_admin("an-inv")
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.get(
        "/api/admin/numbers?assignment=bogus", cookies=cookies, headers=headers
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_query"


async def test_list_team_id_with_unassigned_conflict_400(client):
    admin = await _super_admin("an-conf")
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.get(
        "/api/admin/numbers?assignment=unassigned&team_id=1",
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_query"


async def test_admin_numbers_forbidden_for_member(client):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "an-fb")
            await seed_user(s, username="an-fbl", role="group_leader", team_id=tid)
            m = await seed_user(s, username="an-fbm", role="group_member", team_id=tid)
    cookies, headers = await make_auth(m.id, "group_member", tid)
    r = await client.get("/api/admin/numbers", cookies=cookies, headers=headers)
    assert r.status_code == 403


# --- PATCH assign/reassign/unassign -----------------------------------------


async def test_patch_assign_and_unassign(client):
    admin = await _super_admin("an-pa")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "an-pt")
            await seed_user(s, username="an-pl", role="group_leader", team_id=tid)
            num_id = (
                await s.execute(
                    text(
                        "INSERT INTO phone_numbers (phone_number, team_id, is_active) "
                        "VALUES ('+441240000010', NULL, true) RETURNING id"
                    )
                )
            ).scalar()
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    # assign
    r1 = await client.patch(
        f"/api/admin/numbers/{num_id}",
        json={"team_id": tid},
        cookies=cookies,
        headers=headers,
    )
    assert r1.status_code == 200
    assert r1.json()["team_id"] == tid
    assert await _val("SELECT team_id FROM phone_numbers WHERE id=:i", i=num_id) == tid
    # unassign (team_id: null)
    r2 = await client.patch(
        f"/api/admin/numbers/{num_id}",
        json={"team_id": None},
        cookies=cookies,
        headers=headers,
    )
    assert r2.status_code == 200
    assert r2.json()["team_id"] is None
    assert r2.json()["team_name"] is None
    assert await _val("SELECT team_id FROM phone_numbers WHERE id=:i", i=num_id) is None
    # audit
    assert (
        await _val(
            "SELECT count(*) FROM admin_audit WHERE action='number_team_assigned'"
        )
        >= 2
    )


async def test_patch_number_not_found_404(client):
    admin = await _super_admin("an-nf")
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        "/api/admin/numbers/999999",
        json={"team_id": None},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 404
    assert r.json()["error"] == "number_not_found"


async def test_patch_team_not_found_404(client):
    admin = await _super_admin("an-tnf")
    async with make_session() as s:
        async with s.begin():
            num_id = (
                await s.execute(
                    text(
                        "INSERT INTO phone_numbers (phone_number, team_id, is_active) "
                        "VALUES ('+441240000011', NULL, true) RETURNING id"
                    )
                )
            ).scalar()
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/admin/numbers/{num_id}",
        json={"team_id": 987654},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 404
    assert r.json()["error"] == "team_not_found"


async def test_patch_requires_csrf(client):
    admin = await _super_admin("an-csrf")
    async with make_session() as s:
        async with s.begin():
            num_id = (
                await s.execute(
                    text(
                        "INSERT INTO phone_numbers (phone_number, team_id, is_active) "
                        "VALUES ('+441240000012', NULL, true) RETURNING id"
                    )
                )
            ).scalar()
    cookies, _ = await make_auth(admin, "super_admin", None)
    # без X-CSRF-Token
    r = await client.patch(
        f"/api/admin/numbers/{num_id}",
        json={"team_id": None},
        cookies=cookies,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "csrf_failed"


async def test_no_js_method_override_patch_succeeds(client):
    """no-JS POST + _method=PATCH на /api/admin/numbers/{id} → 200 (whitelist)."""
    admin = await _super_admin("an-mo")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "an-mo-t")
            await seed_user(s, username="an-mol", role="group_leader", team_id=tid)
            num_id = (
                await s.execute(
                    text(
                        "INSERT INTO phone_numbers (phone_number, team_id, is_active) "
                        "VALUES ('+441240000013', NULL, true) RETURNING id"
                    )
                )
            ).scalar()
    cookies, headers = await make_auth(admin, "super_admin", None)
    csrf = headers["X-CSRF-Token"]
    r = await client.post(
        f"/api/admin/numbers/{num_id}",
        content=f"_method=PATCH&team_id={tid}&csrf_token={csrf}",
        cookies=cookies,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("team_id") == tid
    assert await _val("SELECT team_id FROM phone_numbers WHERE id=:i", i=num_id) == tid
