"""Integration: PATCH /api/numbers/{id} — никнейм номера (docs/05 §6, Feature 1).

Presence-семантика (ключ label): отсутствует → no-op 200; присутствует+непустой
(после strip) → set; присутствует+пустой/whitespace/null → затирание NULL.
Guard принадлежности (как DELETE): super_admin — любой (вкл. unassigned);
участник — только свои команды (team_id ∈ scope.team_ids), иначе 403.
no-JS fallback: POST + _method=PATCH + label + csrf_token. POST /api/numbers
201 несёт label. Audit number_label_set.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.infrastructure.repositories import PhoneNumberRepository
from shared.db import make_session
from tests.conftest import make_auth, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _team_member(name: str):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, name)
            await seed_user(s, username=name + "l", role="group_leader", team_id=tid)
            m = await seed_user(
                s, username=name + "m", role="group_member", team_id=tid
            )
        return tid, m.id


async def _super_admin(name: str) -> int:
    async with make_session() as s:
        async with s.begin():
            u = await seed_user(s, username=name, role="super_admin", team_id=None)
        return u.id


async def _seed_number(phone: str, team_id: int | None, label: str | None = None):
    async with make_session() as s:
        async with s.begin():
            num = await PhoneNumberRepository(s).create(
                phone_number=phone,
                team_id=team_id,
                added_by_user_id=None,
                label=label,
            )
        return num.id


async def _db_label(number_id: int) -> str | None:
    async with make_session() as s:
        return (
            await s.execute(
                text("SELECT label FROM phone_numbers WHERE id=:i"), {"i": number_id}
            )
        ).scalar()


# --- Presence-семантика ------------------------------------------------------


async def test_label_absent_key_is_noop(client):
    """Ключ label отсутствует в body → no-op 200, label не изменился."""
    tid, mid = await _team_member("lbl-noop")
    nid = await _seed_number("+441270000001", tid, label="Original")
    cookies, headers = await make_auth(mid, "group_member", tid)
    headers["content-type"] = "application/json"
    # Body БЕЗ ключа label → set_label = ("label" in body) = False → no-op.
    r = await client.patch(
        f"/api/numbers/{nid}", json={}, cookies=cookies, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "Original"
    assert await _db_label(nid) == "Original"


async def test_label_set_trimmed(client):
    """Присутствует непустой (после strip) → set, ответ serialize_number с label."""
    tid, mid = await _team_member("lbl-set")
    nid = await _seed_number("+441270000002", tid, label=None)
    cookies, headers = await make_auth(mid, "group_member", tid)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/numbers/{nid}",
        json={"label": "  Support  "},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["label"] == "Support"
    # serialize_number-форма (полный набор полей).
    assert set(body) >= {
        "id",
        "phone_number",
        "team_id",
        "team_name",
        "label",
        "is_active",
        "added_by_user_id",
        "created_at",
    }
    assert await _db_label(nid) == "Support"


@pytest.mark.parametrize("value", ["", "   ", None])
async def test_label_cleared_to_null(client, value):
    """Присутствует пустой/whitespace/null → затирание label=NULL."""
    tid, mid = await _team_member(f"lbl-clr{'n' if value is None else len(value)}")
    nid = await _seed_number("+44127000010" + str(abs(hash(str(value))) % 10), tid, "X")
    cookies, headers = await make_auth(mid, "group_member", tid)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/numbers/{nid}", json={"label": value}, cookies=cookies, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["label"] is None
    assert await _db_label(nid) is None


# --- Guard принадлежности ----------------------------------------------------


async def test_member_own_team_number_200(client):
    tid, mid = await _team_member("lbl-own")
    nid = await _seed_number("+441270000020", tid)
    cookies, headers = await make_auth(mid, "group_member", tid)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/numbers/{nid}", json={"label": "Mine"}, cookies=cookies, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["label"] == "Mine"


async def test_member_foreign_number_403(client):
    tid_a, mid_a = await _team_member("lbl-fa")
    tid_b, _ = await _team_member("lbl-fb")
    nid = await _seed_number("+441270000021", tid_b)
    cookies, headers = await make_auth(mid_a, "group_member", tid_a)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/numbers/{nid}", json={"label": "x"}, cookies=cookies, headers=headers
    )
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"


async def test_member_unassigned_number_403(client):
    tid, mid = await _team_member("lbl-un")
    nid = await _seed_number("+441270000022", None)  # team_id NULL (unassigned)
    cookies, headers = await make_auth(mid, "group_member", tid)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/numbers/{nid}", json={"label": "x"}, cookies=cookies, headers=headers
    )
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"


async def test_super_admin_any_number_incl_unassigned_200(client):
    admin_id = await _super_admin("lbl-root")
    nid = await _seed_number("+441270000023", None)  # unassigned
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/numbers/{nid}",
        json={"label": "Pool"},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "Pool"
    assert r.json()["team_id"] is None


async def test_patch_missing_number_404(client):
    admin_id = await _super_admin("lbl-404")
    cookies, headers = await make_auth(admin_id, "super_admin", None)
    headers["content-type"] = "application/json"
    r = await client.patch(
        "/api/numbers/999999", json={"label": "x"}, cookies=cookies, headers=headers
    )
    assert r.status_code == 404
    assert r.json()["error"] == "number_not_found"


# --- Валидация ---------------------------------------------------------------


async def test_label_too_long_400(client):
    tid, mid = await _team_member("lbl-long")
    nid = await _seed_number("+441270000030", tid)
    cookies, headers = await make_auth(mid, "group_member", tid)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/numbers/{nid}",
        json={"label": "x" * 101},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "validation_error"


# --- Audit -------------------------------------------------------------------


async def test_label_set_writes_audit(client):
    tid, mid = await _team_member("lbl-aud")
    nid = await _seed_number("+441270000031", tid)
    cookies, headers = await make_auth(mid, "group_member", tid)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/numbers/{nid}", json={"label": "Tag"}, cookies=cookies, headers=headers
    )
    assert r.status_code == 200
    async with make_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT details FROM admin_audit "
                    "WHERE action='number_label_set' ORDER BY id DESC LIMIT 1"
                )
            )
        ).scalar()
    assert row is not None
    details = row if isinstance(row, dict) else json.loads(row)
    assert details["number_id"] == nid
    assert details["label"] == "Tag"
    assert details["phone_number"] == "+441270000031"


# --- no-JS fallback (POST + _method=PATCH) -----------------------------------


async def test_label_no_js_method_override(client):
    tid, mid = await _team_member("lbl-nojs")
    nid = await _seed_number("+441270000032", tid)
    cookies, headers = await make_auth(mid, "group_member", tid)
    csrf = headers["X-CSRF-Token"]
    # Реальный запрос, который генерирует no-JS форма: POST на тот же путь ресурса.
    r = await client.post(
        f"/api/numbers/{nid}",
        content=f"_method=PATCH&label=Desk&csrf_token={csrf}",
        cookies=cookies,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["label"] == "Desk"
    assert body["id"] == nid
    assert await _db_label(nid) == "Desk"


async def test_label_no_js_empty_clears(client):
    """no-JS форма всегда шлёт label=<value>; пустое поле ⇒ затирание в NULL (§6)."""
    tid, mid = await _team_member("lbl-nojsc")
    nid = await _seed_number("+441270000033", tid, label="WillGo")
    cookies, headers = await make_auth(mid, "group_member", tid)
    csrf = headers["X-CSRF-Token"]
    r = await client.post(
        f"/api/numbers/{nid}",
        content=f"_method=PATCH&label=&csrf_token={csrf}",
        cookies=cookies,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["label"] is None
    assert await _db_label(nid) is None


# --- POST /api/numbers 201 несёт label ---------------------------------------


async def test_create_number_response_contains_label_value(client):
    tid, mid = await _team_member("lbl-cv")
    cookies, headers = await make_auth(mid, "group_member", tid)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/numbers",
        json={"phone_number": "+441270000040", "label": "Hotline"},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # Task-scope §6: ответ 201 несёт label (значение или null).
    assert "label" in body
    assert body["label"] == "Hotline"


async def test_patch_non_dict_json_body_400(client):
    """_read_body: JSON-тело не объект (массив) → 400 validation_error (sad-path)."""
    tid, mid = await _team_member("lbl-baddoc")
    nid = await _seed_number("+441270000050", tid)
    cookies, headers = await make_auth(mid, "group_member", tid)
    headers["content-type"] = "application/json"
    r = await client.patch(
        f"/api/numbers/{nid}",
        json=["not", "a", "dict"],
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "validation_error"


async def test_create_number_response_label_null_when_absent(client):
    tid, mid = await _team_member("lbl-cn")
    cookies, headers = await make_auth(mid, "group_member", tid)
    headers["content-type"] = "application/json"
    r = await client.post(
        "/api/numbers",
        json={"phone_number": "+441270000041"},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert "label" in body
    assert body["label"] is None
