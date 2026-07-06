"""Integration: POST /api/admin/users с extra_team_ids (docs/05 §4, ADR-0012, Feature 2).

Создание пользователя сразу в нескольких командах: home + доп. членства. Дедуп,
исключение дубля home, только положительные. Несуществующая доп. команда → 404 +
полный откат (user НЕ создан). Роль/лидерство доп. команд не меняются; «первый=
лидер» только для home. no-JS (getlist) и JSON-массив дают те же членства. Audit
details.extra_team_ids.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from shared.db import make_session
from tests.conftest import make_auth, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _super_admin(name: str) -> int:
    async with make_session() as s:
        async with s.begin():
            u = await seed_user(s, username=name, role="super_admin", team_id=None)
        return u.id


async def _team(name: str, *, with_leader: bool = False):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, name)
            leader_id = None
            if with_leader:
                lead = await seed_user(
                    s, username=name + "-ldr", role="group_leader", team_id=tid
                )
                leader_id = lead.id
        return tid, leader_id


async def _teams_of_user(uid: int) -> set[int]:
    async with make_session() as s:
        rows = (
            await s.execute(
                text("SELECT team_id FROM user_teams WHERE user_id=:u"), {"u": uid}
            )
        ).all()
    return {int(r[0]) for r in rows}


async def _user_by_username(username: str):
    async with make_session() as s:
        return (
            await s.execute(
                text("SELECT id, role, team_id FROM users WHERE username=:n"),
                {"n": username},
            )
        ).first()


# --- Создание с extra: 3 членства -------------------------------------------


async def test_create_with_extra_teams_makes_three_memberships(client):
    admin = await _super_admin("xt-adm1")
    home, _ = await _team("xt-home1", with_leader=True)
    a, _ = await _team("xt-a1", with_leader=True)
    b, _ = await _team("xt-b1", with_leader=True)
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/users",
        json={"username": "xt-newu1", "team_id": home, "extra_team_ids": [a, b]},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert await _teams_of_user(uid) == {home, a, b}


async def test_created_user_visible_in_multiple_chips_on_admin(client):
    admin = await _super_admin("xt-adm-ui")
    home, _ = await _team("xt-home-ui", with_leader=True)
    a, _ = await _team("xt-a-ui", with_leader=True)
    b, _ = await _team("xt-b-ui", with_leader=True)
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/users",
        json={"username": "xt-newu-ui", "team_id": home, "extra_team_ids": [a, b]},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 201, r.text
    # SSR /admin: пользователь показан ОДНОЙ строкой, все команды — чипами.
    page = await client.get("/admin", cookies=cookies)
    assert page.status_code == 200
    body = page.text
    assert "team-chip" in body
    for name in ("xt-home-ui", "xt-a-ui", "xt-b-ui"):
        assert name in body, f"команда {name} не показана чипом на /admin"
    # Ровно ОДНА строка пользователя (не дублируется на членство, ADR-0012):
    # эффективное имя рендерится один раз в ячейке name-text.
    import re as _re

    rows = _re.findall(r'admin-users-table__name-text">\s*xt-newu-ui\b', body)
    assert len(rows) == 1, f"ожидалась одна строка пользователя, найдено {len(rows)}"


# --- Дедуп / исключение home / только положительные --------------------------


async def test_extra_dedup_excludes_home_and_nonpositive(client):
    admin = await _super_admin("xt-adm2")
    home, _ = await _team("xt-home2", with_leader=True)
    a, _ = await _team("xt-a2", with_leader=True)
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    # extra содержит дубли a, дубль home, ноль и отрицательное → останется {home, a}.
    r = await client.post(
        "/api/admin/users",
        json={
            "username": "xt-newu2",
            "team_id": home,
            "extra_team_ids": [a, a, home, 0, -7],
        },
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert await _teams_of_user(uid) == {home, a}


# --- Несуществующая доп. команда → 404 + полный откат ------------------------


async def test_extra_nonexistent_team_404_and_no_user_created(client):
    admin = await _super_admin("xt-adm3")
    home, _ = await _team("xt-home3", with_leader=True)
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/users",
        json={
            "username": "xt-newu3",
            "team_id": home,
            "extra_team_ids": [987654],
        },
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 404
    assert r.json()["error"] == "team_not_found"
    # Полный откат: пользователь НЕ создан.
    assert await _user_by_username("xt-newu3") is None


# --- Роль/лидерство доп. команд не меняются; «первый=лидер» только home -------


async def test_extra_membership_does_not_change_role_or_leadership(client):
    admin = await _super_admin("xt-adm4")
    # home пустая (leader NULL) → новый пользователь станет её лидером.
    home, _ = await _team("xt-home4")
    # доп. команды с уже назначенными лидерами — они НЕ должны смениться.
    a, a_leader = await _team("xt-a4", with_leader=True)
    b, b_leader = await _team("xt-b4", with_leader=True)
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/users",
        json={"username": "xt-newu4", "team_id": home, "extra_team_ids": [a, b]},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    async with make_session() as s:
        # «первый=лидер» сработал ТОЛЬКО для home.
        home_leader = (
            await s.execute(
                text("SELECT leader_user_id FROM teams WHERE id=:t"), {"t": home}
            )
        ).scalar()
        a_leader_now = (
            await s.execute(
                text("SELECT leader_user_id FROM teams WHERE id=:t"), {"t": a}
            )
        ).scalar()
        b_leader_now = (
            await s.execute(
                text("SELECT leader_user_id FROM teams WHERE id=:t"), {"t": b}
            )
        ).scalar()
        role = (
            await s.execute(text("SELECT role FROM users WHERE id=:u"), {"u": uid})
        ).scalar()
    assert home_leader == uid  # первый=лидер для home
    assert role == "group_leader"  # роль — по home
    assert a_leader_now == a_leader  # лидер доп. команды не сменился
    assert b_leader_now == b_leader


# --- no-JS форма (getlist) ---------------------------------------------------


async def test_no_js_form_extra_team_ids_getlist(client):
    admin = await _super_admin("xt-adm5")
    home, _ = await _team("xt-home5", with_leader=True)
    a, _ = await _team("xt-a5", with_leader=True)
    b, _ = await _team("xt-b5", with_leader=True)
    cookies, headers = await make_auth(admin, "super_admin", None)
    csrf = headers["X-CSRF-Token"]
    # Реальный no-JS сабмит: multi-value extra_team_ids[] (getlist), csrf в body.
    form = (
        f"username=xt-newu5&team_id={home}"
        f"&extra_team_ids[]={a}&extra_team_ids[]={b}"
        f"&csrf_token={csrf}"
    )
    r = await client.post(
        "/api/admin/users",
        content=form,
        cookies=cookies,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert await _teams_of_user(uid) == {home, a, b}


# --- Audit details.extra_team_ids -------------------------------------------


async def test_audit_details_include_extra_team_ids(client):
    admin = await _super_admin("xt-adm6")
    home, _ = await _team("xt-home6", with_leader=True)
    a, _ = await _team("xt-a6", with_leader=True)
    b, _ = await _team("xt-b6", with_leader=True)
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/users",
        json={"username": "xt-newu6", "team_id": home, "extra_team_ids": [a, b]},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    async with make_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT details FROM admin_audit "
                    "WHERE action='create_user' AND target_user_id=:u"
                ),
                {"u": uid},
            )
        ).scalar()
    assert row is not None
    details = row if isinstance(row, dict) else json.loads(row)
    assert details["team_id"] == home
    assert sorted(details["extra_team_ids"]) == sorted([a, b])
