"""Integration: SSR /admin сгруппирован + GET /api/admin/users is_leader/сортировка (docs/06 §4)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from shared.db import make_session
from tests.conftest import make_auth, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_admin_dashboard_renders_200_grouped(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="db-root", role="super_admin", team_id=None
            )
            tid = await seed_team(s, "db-team")
            await seed_user(s, username="db-lead", role="group_leader", team_id=tid)
            await seed_user(s, username="db-mem", role="group_member", team_id=tid)
            # unassigned номер в пуле
            await s.execute(
                text(
                    "INSERT INTO phone_numbers (phone_number, team_id, is_active) "
                    "VALUES ('+441250000001', NULL, true)"
                )
            )
    cookies, _ = await make_auth(admin.id, "super_admin", None)
    r = await client.get("/admin", cookies=cookies)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    # сгруппированный контекст отражается в разметке команды и пула
    assert "db-team" in body
    assert "+441250000001" in body


async def test_admin_users_api_has_is_leader_and_sorted(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="ul-root", role="super_admin", team_id=None
            )
            tid = await seed_team(s, "ul-team")
            await seed_user(s, username="ul-lead", role="group_leader", team_id=tid)
            await seed_user(s, username="ul-zeta", role="group_member", team_id=tid)
    cookies, headers = await make_auth(admin.id, "super_admin", None)
    r = await client.get("/api/admin/users", cookies=cookies, headers=headers)
    assert r.status_code == 200
    users = r.json()["users"]
    by_name = {u["username"]: u for u in users}
    assert "is_leader" in by_name["ul-lead"]
    assert by_name["ul-lead"]["is_leader"] is True
    assert by_name["ul-zeta"]["is_leader"] is False
    # В пределах команды лидер идёт раньше участника.
    team_users = [u for u in users if u["team_id"] == tid]
    assert team_users[0]["username"] == "ul-lead"
