"""Integration: мультичленство M:N (ADR-0012), сценарии docs/06 §33–41."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.application.services import handle_incoming_sms
from shared.config import get_settings
from shared.db import make_session
from tests.conftest import (
    FakeTelegram,
    make_auth,
    seed_link,
    seed_membership,
    seed_number,
    seed_team,
    seed_user,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _super_admin(name):
    async with make_session() as s:
        async with s.begin():
            u = await seed_user(s, username=name, role="super_admin", team_id=None)
        return u.id


async def _count(sql, **p):
    async with make_session() as s:
        return int((await s.execute(text(sql), p)).scalar() or 0)


# --- §33: recipients через членство -----------------------------------------


async def test_recipient_via_additional_membership_gets_team_sms():
    """U (home=S) с доп.-членством T + живая привязка → SMS на номер T доставляется U."""
    async with make_session() as s:
        async with s.begin():
            home = await seed_team(s, "mt-home1")
            t = await seed_team(s, "mt-teamT1")
            u = await seed_user(s, username="mt-u1", role="group_leader", team_id=home)
            # лидер T без привязки (чтобы получателем был именно U).
            await seed_user(s, username="mt-tl1", role="group_leader", team_id=t)
            await seed_membership(s, user_id=u.id, team_id=t)
            await seed_link(s, telegram_user_id=71001, user_id=u.id)
            await seed_number(s, phone="+441290000001", team_id=t)
    fake = FakeTelegram()
    async with make_session() as s:
        await handle_incoming_sms(
            s,
            fake,
            get_settings(),
            twilio_message_sid="MT1",
            from_number="+1",
            to_number="+441290000001",
            body="hi",
            raw_payload={},
        )
    assert await _count("SELECT count(*) FROM deliveries WHERE status='sent'") == 1
    assert fake.calls and fake.calls[0][0] == 71001


async def test_member_of_two_teams_receives_both():
    async with make_session() as s:
        async with s.begin():
            home = await seed_team(s, "mt-home2")
            t = await seed_team(s, "mt-teamT2")
            u = await seed_user(s, username="mt-u2", role="group_leader", team_id=home)
            await seed_membership(s, user_id=u.id, team_id=t)
            await seed_link(s, telegram_user_id=72001, user_id=u.id)
            await seed_number(s, phone="+441290000002", team_id=home)
            await seed_number(s, phone="+441290000003", team_id=t)
    fake = FakeTelegram()
    async with make_session() as s:
        await handle_incoming_sms(
            s,
            fake,
            get_settings(),
            twilio_message_sid="MT2a",
            from_number="+1",
            to_number="+441290000002",
            body="home",
            raw_payload={},
        )
        await handle_incoming_sms(
            s,
            fake,
            get_settings(),
            twilio_message_sid="MT2b",
            from_number="+1",
            to_number="+441290000003",
            body="teamT",
            raw_payload={},
        )
    # Обе SMS доставлены U (по одной на команду), дедуп на chat в рамках одной SMS.
    assert await _count("SELECT count(*) FROM deliveries WHERE status='sent'") == 2
    assert {c[0] for c in fake.calls} == {72001}
    assert len(fake.calls) == 2


# --- §34: membership endpoints ----------------------------------------------


async def _seed_admin_user_team():
    admin = await _super_admin("mt-adm")
    async with make_session() as s:
        async with s.begin():
            home = await seed_team(s, "mt-mh")
            extra = await seed_team(s, "mt-me")
            await seed_user(s, username="mt-mel", role="group_leader", team_id=extra)
            u = await seed_user(s, username="mt-mu", role="group_leader", team_id=home)
        return admin, u.id, home, extra


async def test_add_membership_201(client):
    admin, uid, home, extra = await _seed_admin_user_team()
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        f"/api/admin/users/{uid}/teams",
        json={"team_id": extra},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert (
        await _count(
            "SELECT count(*) FROM user_teams WHERE user_id=:u AND team_id=:t",
            u=uid,
            t=extra,
        )
        == 1
    )
    # audit-запись создаётся (user_team_add в ALLOWED_ACTIONS).
    assert (
        await _count(
            "SELECT count(*) FROM admin_audit "
            "WHERE action='user_team_add' AND target_user_id=:u",
            u=uid,
        )
        == 1
    )


async def test_add_membership_already_exists_409(client):
    admin, uid, home, extra = await _seed_admin_user_team()
    async with make_session() as s:
        async with s.begin():
            await seed_membership(s, user_id=uid, team_id=extra)
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        f"/api/admin/users/{uid}/teams",
        json={"team_id": extra},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 409
    assert r.json()["error"] == "membership_already_exists"


async def test_add_membership_super_admin_400(client):
    admin, uid, home, extra = await _seed_admin_user_team()
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        f"/api/admin/users/{admin}/teams",
        json={"team_id": extra},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "cannot_add_super_admin_to_team"


async def test_add_membership_team_not_found_404(client):
    admin, uid, home, extra = await _seed_admin_user_team()
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        f"/api/admin/users/{uid}/teams",
        json={"team_id": 987654},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 404
    assert r.json()["error"] == "team_not_found"


async def test_add_membership_user_not_found_404(client):
    admin, uid, home, extra = await _seed_admin_user_team()
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/users/987654/teams",
        json={"team_id": extra},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 404
    assert r.json()["error"] == "user_not_found"


async def test_remove_membership_204(client):
    admin, uid, home, extra = await _seed_admin_user_team()
    async with make_session() as s:
        async with s.begin():
            await seed_membership(s, user_id=uid, team_id=extra)
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.request(
        "DELETE",
        f"/api/admin/users/{uid}/teams/{extra}",
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 204, r.text
    assert (
        await _count(
            "SELECT count(*) FROM user_teams WHERE user_id=:u AND team_id=:t",
            u=uid,
            t=extra,
        )
        == 0
    )
    # audit-запись создаётся (user_team_remove в ALLOWED_ACTIONS).
    assert (
        await _count(
            "SELECT count(*) FROM admin_audit "
            "WHERE action='user_team_remove' AND target_user_id=:u",
            u=uid,
        )
        == 1
    )


async def test_remove_home_membership_400(client):
    admin, uid, home, extra = await _seed_admin_user_team()
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.request(
        "DELETE",
        f"/api/admin/users/{uid}/teams/{home}",
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "cannot_remove_home_membership"


async def test_remove_membership_not_found_404(client):
    admin, uid, home, extra = await _seed_admin_user_team()
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.request(
        "DELETE",
        f"/api/admin/users/{uid}/teams/{extra}",
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 404
    assert r.json()["error"] == "membership_not_found"


async def test_remove_membership_no_js_method_override(client):
    admin, uid, home, extra = await _seed_admin_user_team()
    async with make_session() as s:
        async with s.begin():
            await seed_membership(s, user_id=uid, team_id=extra)
    cookies, headers = await make_auth(admin, "super_admin", None)
    csrf = headers["X-CSRF-Token"]
    r = await client.post(
        f"/api/admin/users/{uid}/teams/{extra}",
        content=f"_method=DELETE&csrf_token={csrf}",
        cookies=cookies,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code in (200, 204), r.text
    assert (
        await _count(
            "SELECT count(*) FROM user_teams WHERE user_id=:u AND team_id=:t",
            u=uid,
            t=extra,
        )
        == 0
    )


# --- §35: leader guard home-based -------------------------------------------


async def test_delete_leader_with_only_additional_members_passes(client):
    """Лидер T, где кроме него только доп.-участники (home пусто) → delete проходит."""
    admin = await _super_admin("mt-lg1")
    async with make_session() as s:
        async with s.begin():
            t = await seed_team(s, "mt-lgT1")
            s_home = await seed_team(s, "mt-lgS1")
            leader = await seed_user(
                s, username="mt-lgL1", role="group_leader", team_id=t
            )
            other = await seed_user(
                s, username="mt-lgO1", role="group_leader", team_id=s_home
            )
            await seed_membership(s, user_id=other.id, team_id=t)  # доп.-участник T
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.request(
        "DELETE", f"/api/admin/users/{leader.id}", cookies=cookies, headers=headers
    )
    assert r.status_code == 200, r.text  # доп.-участник не блокирует


async def test_delete_leader_with_other_home_members_409(client):
    admin = await _super_admin("mt-lg2")
    async with make_session() as s:
        async with s.begin():
            t = await seed_team(s, "mt-lgT2")
            leader = await seed_user(
                s, username="mt-lgL2", role="group_leader", team_id=t
            )
            await seed_user(s, username="mt-lgM2", role="group_member", team_id=t)
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.request(
        "DELETE", f"/api/admin/users/{leader.id}", cookies=cookies, headers=headers
    )
    assert r.status_code == 409
    assert r.json()["error"] == "user_is_leader"


# --- §36: disband с доп.-участниками -----------------------------------------


async def test_disband_team_with_only_additional_members(client):
    admin = await _super_admin("mt-db")
    async with make_session() as s:
        async with s.begin():
            t = await seed_team(s, "mt-dbT")  # leader NULL, home пусто
            s_home = await seed_team(s, "mt-dbS")
            a = await seed_user(
                s, username="mt-dbA", role="group_leader", team_id=s_home
            )
            await seed_membership(s, user_id=a.id, team_id=t)
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.request(
        "DELETE", f"/api/admin/teams/{t}", cookies=cookies, headers=headers
    )
    assert r.status_code == 200, r.text
    # CASCADE снял user_teams для T; сама команда удалена.
    assert await _count("SELECT count(*) FROM teams WHERE id=:t", t=t) == 0
    assert await _count("SELECT count(*) FROM user_teams WHERE team_id=:t", t=t) == 0


# --- §37: create_user зеркалит home; PATCH move синхронизирует ----------------


async def test_create_user_mirrors_home_membership(client):
    admin = await _super_admin("mt-cu")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "mt-cuT")
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/admin/users",
        json={"username": "mt-newu", "team_id": tid},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 201
    uid = r.json()["id"]
    assert (
        await _count(
            "SELECT count(*) FROM user_teams WHERE user_id=:u AND team_id=:t",
            u=uid,
            t=tid,
        )
        == 1
    )


async def test_move_syncs_memberships_keeps_additional(client):
    admin = await _super_admin("mt-mv")
    async with make_session() as s:
        async with s.begin():
            a = await seed_team(s, "mt-mvA")
            b = await seed_team(s, "mt-mvB")
            extra = await seed_team(s, "mt-mvX")
            await seed_user(s, username="mt-mvAL", role="group_leader", team_id=a)
            await seed_user(s, username="mt-mvBL", role="group_leader", team_id=b)
            mem = await seed_user(s, username="mt-mvM", role="group_member", team_id=a)
            await seed_membership(s, user_id=mem.id, team_id=extra)  # доп.
    cookies, headers = await make_auth(admin, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/admin/users/{mem.id}",
        json={"team_id": b},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 200, r.text
    # home перенесён A→B: членство A снято, B добавлено, доп. extra сохранено.
    async with make_session() as s:
        rows = {
            int(r[0])
            for r in (
                await s.execute(
                    text("SELECT team_id FROM user_teams WHERE user_id=:u"),
                    {"u": mem.id},
                )
            ).all()
        }
    assert rows == {b, extra}


# --- §38: /app + /api/numbers по team_ids ------------------------------------


async def test_app_shows_numbers_of_all_teams(client):
    async with make_session() as s:
        async with s.begin():
            home = await seed_team(s, "mt-appS")
            t = await seed_team(s, "mt-appT")
            await seed_user(s, username="mt-appL", role="group_leader", team_id=t)
            mem = await seed_user(
                s, username="mt-appM", role="group_member", team_id=home
            )
            await seed_membership(s, user_id=mem.id, team_id=t)
            await seed_number(s, phone="+441291000001", team_id=home)
            await seed_number(s, phone="+441291000002", team_id=t)
    cookies, _ = await make_auth(mem.id, "group_member", home)
    r = await client.get("/app", cookies=cookies)
    assert r.status_code == 200
    body = r.text
    assert "+441291000001" in body
    assert "+441291000002" in body


async def test_create_number_in_scope_and_out_of_scope(client):
    async with make_session() as s:
        async with s.begin():
            home = await seed_team(s, "mt-cnS")
            t = await seed_team(s, "mt-cnT")
            other = await seed_team(s, "mt-cnOther")
            await seed_user(s, username="mt-cnL", role="group_leader", team_id=t)
            await seed_user(s, username="mt-cnOL", role="group_leader", team_id=other)
            mem = await seed_user(
                s, username="mt-cnM", role="group_member", team_id=home
            )
            await seed_membership(s, user_id=mem.id, team_id=t)
    cookies, headers = await make_auth(mem.id, "group_member", home)
    headers["content-type"] = "application/json"
    # team_id в scope (доп. команда T) → 201.
    r_ok = await client.post(
        "/api/numbers",
        json={"phone_number": "+441291000010", "team_id": t},
        cookies=cookies,
        headers=headers,
    )
    assert r_ok.status_code == 201, r_ok.text
    # team_id вне scope → 403.
    r_bad = await client.post(
        "/api/numbers",
        json={"phone_number": "+441291000011", "team_id": other},
        cookies=cookies,
        headers=headers,
    )
    assert r_bad.status_code == 403


# --- §39: grouped_dashboard / GET admin/users --------------------------------


async def test_admin_users_returns_team_ids_multi(client):
    admin = await _super_admin("mt-du")
    async with make_session() as s:
        async with s.begin():
            home = await seed_team(s, "mt-duS")
            t = await seed_team(s, "mt-duT")
            await seed_user(s, username="mt-duL", role="group_leader", team_id=t)
            mem = await seed_user(
                s, username="mt-duM", role="group_member", team_id=home
            )
            await seed_membership(s, user_id=mem.id, team_id=t)
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.get("/api/admin/users", cookies=cookies, headers=headers)
    assert r.status_code == 200
    entry = next(u for u in r.json()["users"] if u["username"] == "mt-dum")
    assert "team_ids" in entry
    assert sorted(entry["team_ids"]) == sorted([home, t])
    assert entry["is_leader"] is False


# --- §41: banding /admin -----------------------------------------------------


async def test_admin_dashboard_banding_alternates(client):
    admin = await _super_admin("mt-band")
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "mt-band1")
            t2 = await seed_team(s, "mt-band2")
            await seed_user(s, username="mt-band-l1", role="group_leader", team_id=t1)
            await seed_user(s, username="mt-band-l2", role="group_leader", team_id=t2)
    cookies, _ = await make_auth(admin, "super_admin", None)
    r = await client.get("/admin", cookies=cookies)
    assert r.status_code == 200
    body = r.text
    assert "admin__group--band-a" in body
    assert "admin__group--band-b" in body  # чередование
    assert "admin__group--no-team" in body  # секция «Администраторы» нейтральна
