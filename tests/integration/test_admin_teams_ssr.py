"""Integration: SSR GET /admin/teams (docs/05 §7, ADR-0016, Feature 3).

Server-rendered таблица команд: под каждой командой её номера по ТЕКУЩЕЙ
принадлежности с эффективным лейблом; команда без номеров → empty-state;
unassigned-номера НЕ показаны. Контекст несёт литеральные имена §7 (teams[...],
numbers_by_team{...}), members_count по user_teams, numbers_count. Контролы
create/rename/delete — фактическим сабмитом (form + _method) до эффекта;
назначение лидера. require_admin: участник → 403.
"""

from __future__ import annotations

import pytest

from app.application.admin_service import AdminService
from shared.db import make_session
from tests.conftest import (
    make_auth,
    seed_membership,
    seed_team,
    seed_user,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _super_admin(name: str) -> int:
    async with make_session() as s:
        async with s.begin():
            u = await seed_user(s, username=name, role="super_admin", team_id=None)
        return u.id


# --- Контекст: литеральные имена, members_count/numbers_count, effective_label -


async def test_teams_page_context_literal_names_and_counts():
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "atx-ctx-A")
            t2 = await seed_team(s, "atx-ctx-B")
            l1 = await seed_user(
                s, username="atx-ctx-l1", role="group_leader", team_id=t1
            )
            l2 = await seed_user(
                s, username="atx-ctx-l2", role="group_leader", team_id=t2
            )
            # l2 — доп.-участник T1 → members_count(T1) по user_teams = 2 (l1 + l2).
            await seed_membership(s, user_id=l2.id, team_id=t1)
            from app.infrastructure.repositories import PhoneNumberRepository

            repo = PhoneNumberRepository(s)
            await repo.create(
                phone_number="+441281000001",
                team_id=t1,
                added_by_user_id=None,
                label="Front",
            )
            await repo.create(
                phone_number="+441281000002",
                team_id=t1,
                added_by_user_id=None,
                label=None,
            )
            # unassigned — не должен попасть в numbers_by_team.
            await repo.create(
                phone_number="+441281009999",
                team_id=None,
                added_by_user_id=None,
                label="Pool",
            )
            l1_username = l1.username

    async with make_session() as s:
        ctx = await AdminService(s).teams_page()

    assert "teams" in ctx and "numbers_by_team" in ctx
    teams = {t["name"]: t for t in ctx["teams"]}
    a = teams["atx-ctx-A"]
    b = teams["atx-ctx-B"]
    # members_count по user_teams (home l1 + доп. l2).
    assert a["members_count"] == 2
    assert a["numbers_count"] == 2
    assert a["leader"] is not None and a["leader"]["username"] == l1_username
    # numbers_by_team: ключ — team_id; effective_label = label or phone_number.
    nbt = ctx["numbers_by_team"]
    a_nums = {n["phone_number"]: n for n in nbt[a["id"]]}
    assert a_nums["+441281000001"]["effective_label"] == "Front"
    assert a_nums["+441281000002"]["effective_label"] == "+441281000002"
    # Команда без номеров присутствует с пустым списком.
    assert nbt[b["id"]] == []
    # unassigned-номер НЕ включён ни в один список.
    all_phones = {n["phone_number"] for lst in nbt.values() for n in lst}
    assert "+441281009999" not in all_phones


# --- SSR-рендер: таблица, номера под командой, empty-state, unassigned скрыт ---


async def test_teams_page_renders_table_numbers_and_hides_unassigned(client):
    admin = await _super_admin("atx-ssr-root")
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "atx-ssr-withnum")
            await seed_user(s, username="atx-ssr-l1", role="group_leader", team_id=t1)
            t2 = await seed_team(s, "atx-ssr-empty")
            await seed_user(s, username="atx-ssr-l2", role="group_leader", team_id=t2)
            from app.infrastructure.repositories import PhoneNumberRepository

            repo = PhoneNumberRepository(s)
            await repo.create(
                phone_number="+441282000001",
                team_id=t1,
                added_by_user_id=None,
                label="MainDesk",
            )
            await repo.create(
                phone_number="+441282009999",
                team_id=None,
                added_by_user_id=None,
                label=None,
            )
    cookies, _ = await make_auth(admin, "super_admin", None)
    r = await client.get("/admin/teams", cookies=cookies)
    assert r.status_code == 200
    body = r.text
    # Единая SSR-таблица команд.
    assert "admin-teams-table" in body
    # Эффективный лейбл номера под командой.
    assert "MainDesk" in body
    assert "+441282000001" in body
    # Команда без номеров → empty-state.
    assert "Номеров в команде пока нет." in body
    # Unassigned-номер НЕ показан на teams-странице (ADR-0016 §3).
    assert "+441282009999" not in body


async def test_teams_page_empty_state_no_teams(client):
    admin = await _super_admin("atx-noteams-root")
    cookies, _ = await make_auth(admin, "super_admin", None)
    r = await client.get("/admin/teams", cookies=cookies)
    assert r.status_code == 200
    assert "Команд пока нет." in r.text


# --- require_admin: участник → 403 ------------------------------------------


async def test_teams_page_forbidden_for_member(client):
    async with make_session() as s:
        async with s.begin():
            t = await seed_team(s, "atx-guard-T")
            m = await seed_user(
                s, username="atx-guard-m", role="group_leader", team_id=t
            )
            uid, tid = m.id, t
    cookies, _ = await make_auth(uid, "group_leader", tid)
    r = await client.get("/admin/teams", cookies=cookies)
    assert r.status_code == 403


# --- Контролы через фактический сабмит (form + _method) → эффект --------------


async def test_create_team_via_form_submit_then_visible(client):
    admin = await _super_admin("atx-create-root")
    cookies, headers = await make_auth(admin, "super_admin", None)
    csrf = headers["X-CSRF-Token"]
    r = await client.post(
        "/api/admin/teams",
        content=f"name=atx-created-team&csrf_token={csrf}",
        cookies=cookies,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 201, r.text
    page = await client.get("/admin/teams", cookies=cookies)
    assert page.status_code == 200
    assert "atx-created-team" in page.text


async def test_rename_team_via_form_method_override_then_visible(client):
    admin = await _super_admin("atx-rename-root")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "atx-oldname")
    cookies, headers = await make_auth(admin, "super_admin", None)
    csrf = headers["X-CSRF-Token"]
    r = await client.post(
        f"/api/admin/teams/{tid}",
        content=f"_method=PATCH&name=atx-newname&csrf_token={csrf}",
        cookies=cookies,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text
    page = await client.get("/admin/teams", cookies=cookies)
    assert "atx-newname" in page.text
    assert "atx-oldname" not in page.text


async def test_delete_empty_team_via_form_method_override_then_gone(client):
    admin = await _super_admin("atx-del-root")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "atx-todelete")  # пустая (leader NULL)
    cookies, headers = await make_auth(admin, "super_admin", None)
    csrf = headers["X-CSRF-Token"]
    r = await client.post(
        f"/api/admin/teams/{tid}",
        content=f"_method=DELETE&csrf_token={csrf}",
        cookies=cookies,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text
    page = await client.get("/admin/teams", cookies=cookies)
    assert "atx-todelete" not in page.text


async def test_assign_leader_reflected_on_page(client):
    admin = await _super_admin("atx-leader-root")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "atx-leaderT")
            # Первый добавленный станет лидером; второй — обычный участник.
            await seed_user(s, username="atx-l-first", role="group_leader", team_id=tid)
            second = await seed_user(
                s, username="atx-l-second", role="group_member", team_id=tid
            )
            second_id = second.id
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/admin/teams/{tid}/leader",
        json={"new_leader_user_id": second_id},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["leader_user_id"] == second_id
    # Новый лидер отражён на SSR-странице (мета-строка «Лидер»).
    page = await client.get("/admin/teams", cookies=cookies)
    assert page.status_code == 200
    assert "atx-l-second" in page.text
