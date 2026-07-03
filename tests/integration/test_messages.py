"""Integration: просмотр входящих SMS — GET /api/messages + SSR /messages.

Покрывает docs/06 §42-52 (ADR-0014, docs/05 §9): ролевая видимость по ТЕКУЩЕЙ
принадлежности номера (phone_numbers.team_id), keyset-пагинация, opaque-курсор,
валидация limit, анти-энумерация, read-only, no-JS fallback.

Реальная БД (conftest). Внешние сервисы не задействованы (read-only просмотр).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.infrastructure.repositories import PhoneNumberRepository, SmsRepository
from shared.db import make_session
from tests.conftest import make_auth, seed_number, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

_BASE_TS = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)


async def _seed_sms(
    s,
    *,
    to_number: str,
    team_id: int | None,
    received_at: datetime,
    from_number: str = "+12025550000",
    body: str = "hi",
    sid: str | None = None,
):
    """Вставить inbound_sms с явным received_at (snapshot team_id — историч.)."""
    return await SmsRepository(s).create(
        twilio_message_sid=sid,
        from_number=from_number,
        to_number=to_number,
        body=body,
        team_id=team_id,
        raw_payload={"MessageSid": sid or "x", "SecretField": "must-not-leak"},
        received_at=received_at,
    )


async def _reassign_number(number_id: int, team_id: int | None) -> None:
    async with make_session() as s:
        async with s.begin():
            await PhoneNumberRepository(s).set_team(
                number_id=number_id, team_id=team_id
            )


async def _admin_auth():
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="msg-root", role="super_admin", team_id=None
            )
    return await make_auth(admin.id, "super_admin", None)


# --- §42 Ролевая видимость super_admin --------------------------------------


async def test_super_admin_sees_all_including_unassigned_and_deleted(client):
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msg-T1")
            t2 = await seed_team(s, "msg-T2")
            await seed_number(s, phone="+441000000001", team_id=t1)
            await seed_number(s, phone="+441000000002", team_id=t2)
            # +441000000003 — номер вне phone_numbers (удалённый/неизвестный).
            await _seed_sms(
                s, to_number="+441000000001", team_id=t1, received_at=_BASE_TS
            )
            await _seed_sms(
                s,
                to_number="+441000000002",
                team_id=t2,
                received_at=_BASE_TS + timedelta(seconds=1),
            )
            await _seed_sms(
                s,
                to_number="+441000000003",
                team_id=None,
                received_at=_BASE_TS + timedelta(seconds=2),
            )
    cookies, _ = await _admin_auth()
    r = await client.get("/api/messages", cookies=cookies)
    assert r.status_code == 200, r.text
    tos = {m["to_number"] for m in r.json()["messages"]}
    # Видны все, включая SMS номера без сопоставления в phone_numbers.
    assert tos == {"+441000000001", "+441000000002", "+441000000003"}


async def test_super_admin_to_number_filter(client):
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msgf-T1")
            await seed_number(s, phone="+441000010001", team_id=t1)
            await seed_number(s, phone="+441000010002", team_id=t1)
            await _seed_sms(
                s, to_number="+441000010001", team_id=t1, received_at=_BASE_TS
            )
            await _seed_sms(
                s,
                to_number="+441000010002",
                team_id=t1,
                received_at=_BASE_TS + timedelta(seconds=1),
            )
    cookies, _ = await _admin_auth()
    r = await client.get(
        "/api/messages", params={"to_number": "+441000010001"}, cookies=cookies
    )
    assert r.status_code == 200
    tos = {m["to_number"] for m in r.json()["messages"]}
    assert tos == {"+441000010001"}


async def test_super_admin_team_id_filter_by_current_ownership(client):
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msgt-T1")
            t2 = await seed_team(s, "msgt-T2")
            await seed_number(s, phone="+441000020001", team_id=t1)
            await seed_number(s, phone="+441000020002", team_id=t2)
            await _seed_sms(
                s, to_number="+441000020001", team_id=t1, received_at=_BASE_TS
            )
            await _seed_sms(
                s,
                to_number="+441000020002",
                team_id=t2,
                received_at=_BASE_TS + timedelta(seconds=1),
            )
            t1_id = t1
    cookies, _ = await _admin_auth()
    r = await client.get("/api/messages", params={"team_id": t1_id}, cookies=cookies)
    assert r.status_code == 200
    tos = {m["to_number"] for m in r.json()["messages"]}
    assert tos == {"+441000020001"}


# --- §43 Ролевая видимость участника ----------------------------------------


async def test_member_sees_only_own_team_numbers(client):
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msgm-T1")
            t2 = await seed_team(s, "msgm-T2")
            await seed_user(s, username="msgm-l1", role="group_leader", team_id=t1)
            member = await seed_user(
                s, username="msgm-m1", role="group_member", team_id=t1
            )
            await seed_user(s, username="msgm-l2", role="group_leader", team_id=t2)
            await seed_number(s, phone="+441000030001", team_id=t1)
            await seed_number(s, phone="+441000030002", team_id=t2)
            await _seed_sms(
                s, to_number="+441000030001", team_id=t1, received_at=_BASE_TS
            )
            await _seed_sms(
                s,
                to_number="+441000030002",
                team_id=t2,
                received_at=_BASE_TS + timedelta(seconds=1),
            )
            member_id, t1_id = member.id, t1
    cookies, _ = await make_auth(member_id, "group_member", t1_id)
    r = await client.get("/api/messages", cookies=cookies)
    assert r.status_code == 200
    tos = {m["to_number"] for m in r.json()["messages"]}
    assert tos == {"+441000030001"}


async def test_member_team_id_query_ignored(client):
    """Участник передаёт ?team_id чужой команды — игнорируется, видит свои."""
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msgi-T1")
            t2 = await seed_team(s, "msgi-T2")
            await seed_user(s, username="msgi-l1", role="group_leader", team_id=t1)
            member = await seed_user(
                s, username="msgi-m1", role="group_member", team_id=t1
            )
            await seed_number(s, phone="+441000040001", team_id=t1)
            await _seed_sms(
                s, to_number="+441000040001", team_id=t1, received_at=_BASE_TS
            )
            member_id, t1_id, t2_id = member.id, t1, t2
    cookies, _ = await make_auth(member_id, "group_member", t1_id)
    r = await client.get("/api/messages", params={"team_id": t2_id}, cookies=cookies)
    assert r.status_code == 200
    tos = {m["to_number"] for m in r.json()["messages"]}
    # team_id проигнорирован → видит SMS своей команды, а не пусто/чужое.
    assert tos == {"+441000040001"}


# --- §44 Текущая принадлежность vs снимок (ключевой сценарий) ----------------


async def test_reassigned_number_makes_history_visible_to_new_team(client):
    """SMS приняты, когда N был в T2/unassigned; N переназначен в T1 участника →
    участник T1 видит историю; участник T2 (откуда N ушёл) — не видит."""
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msgc-T1")
            t2 = await seed_team(s, "msgc-T2")
            await seed_user(s, username="msgc-l1", role="group_leader", team_id=t1)
            u = await seed_user(s, username="msgc-m1", role="group_member", team_id=t1)
            await seed_user(s, username="msgc-l2", role="group_leader", team_id=t2)
            v = await seed_user(s, username="msgc-m2", role="group_member", team_id=t2)
            # N сейчас в T2; SMS принят со снимком team_id=T2.
            n = await seed_number(s, phone="+441000050001", team_id=t2)
            await _seed_sms(
                s, to_number="+441000050001", team_id=t2, received_at=_BASE_TS
            )
            u_id, v_id, t1_id, t2_id, n_id = u.id, v.id, t1, t2, n.id
    # До переназначения: U (T1) не видит; V (T2) видит.
    u_cookies, _ = await make_auth(u_id, "group_member", t1_id)
    v_cookies, _ = await make_auth(v_id, "group_member", t2_id)
    r_u0 = await client.get("/api/messages", cookies=u_cookies)
    r_v0 = await client.get("/api/messages", cookies=v_cookies)
    assert [m["to_number"] for m in r_u0.json()["messages"]] == []
    assert {m["to_number"] for m in r_v0.json()["messages"]} == {"+441000050001"}

    # Переназначить N в T1 (текущая принадлежность).
    await _reassign_number(n_id, t1_id)

    r_u1 = await client.get("/api/messages", cookies=u_cookies)
    r_v1 = await client.get("/api/messages", cookies=v_cookies)
    # U (T1) теперь видит историю по текущей принадлежности; snapshot=T2 игнор.
    assert {m["to_number"] for m in r_u1.json()["messages"]} == {"+441000050001"}
    # V (T2) больше не видит — номер выпал из его scope.
    assert [m["to_number"] for m in r_v1.json()["messages"]] == []


async def test_number_leaving_team_hides_its_sms(client):
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msgl-T1")
            await seed_user(s, username="msgl-l1", role="group_leader", team_id=t1)
            u = await seed_user(s, username="msgl-m1", role="group_member", team_id=t1)
            n = await seed_number(s, phone="+441000060001", team_id=t1)
            await _seed_sms(
                s, to_number="+441000060001", team_id=t1, received_at=_BASE_TS
            )
            u_id, t1_id, n_id = u.id, t1, n.id
    cookies, _ = await make_auth(u_id, "group_member", t1_id)
    r0 = await client.get("/api/messages", cookies=cookies)
    assert {m["to_number"] for m in r0.json()["messages"]} == {"+441000060001"}
    # Номер уходит из команды (unassigned).
    await _reassign_number(n_id, None)
    r1 = await client.get("/api/messages", cookies=cookies)
    assert [m["to_number"] for m in r1.json()["messages"]] == []


# --- §45 Анти-энумерация -----------------------------------------------------


async def test_member_to_number_outside_scope_empty_200(client):
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msge-T1")
            t2 = await seed_team(s, "msge-T2")
            await seed_user(s, username="msge-l1", role="group_leader", team_id=t1)
            member = await seed_user(
                s, username="msge-m1", role="group_member", team_id=t1
            )
            await seed_user(s, username="msge-l2", role="group_leader", team_id=t2)
            await seed_number(s, phone="+441000070001", team_id=t1)
            await seed_number(s, phone="+441000070002", team_id=t2)
            await _seed_sms(
                s, to_number="+441000070002", team_id=t2, received_at=_BASE_TS
            )
            member_id, t1_id = member.id, t1
    cookies, _ = await make_auth(member_id, "group_member", t1_id)
    r = await client.get(
        "/api/messages", params={"to_number": "+441000070002"}, cookies=cookies
    )
    # Чужой номер → 200 с пустым списком (не 403/404), существование не раскрыто.
    assert r.status_code == 200
    assert r.json()["messages"] == []
    assert r.json()["next_cursor"] is None


# --- §46 Пустой scope --------------------------------------------------------


async def test_member_empty_scope_returns_empty():
    """Пустой scope.team_ids (не super_admin) → пустой результат.

    HTTP-путь не порождает пустой scope у member/leader (CHECK-инвариант держит
    домашнюю команду в team_ids), поэтому проверяем сервис напрямую: реальная БД
    с видимыми SMS, но scope участника пуст → ни одна строка не видна.
    """
    from app.application.messages_service import MessageQueryService

    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msgz-T1")
            await seed_number(s, phone="+441000080001", team_id=t1)
            await _seed_sms(
                s, to_number="+441000080001", team_id=t1, received_at=_BASE_TS
            )
    async with make_session() as s:
        page = await MessageQueryService(s).list_messages(
            is_super_admin=False,
            team_ids=frozenset(),
            to_number=None,
            team_id=None,
            cursor=None,
            limit=50,
        )
    assert page.rows == []
    assert page.next_cursor is None


# --- §47 Keyset-пагинация ----------------------------------------------------


async def _collect_all_ids(client, cookies, *, limit, params=None):
    ids: list[int] = []
    cursor = None
    guard = 0
    while True:
        guard += 1
        assert guard < 100, "пагинация не сходится (возможен цикл)"
        p = dict(params or {})
        p["limit"] = limit
        if cursor is not None:
            p["cursor"] = cursor
        r = await client.get("/api/messages", params=p, cookies=cookies)
        assert r.status_code == 200, r.text
        data = r.json()
        page = data["messages"]
        assert len(page) <= limit
        ids.extend(m["id"] for m in page)
        cursor = data["next_cursor"]
        if cursor is None:
            break
        # Промежуточная страница обязана быть полной (limit элементов).
        assert len(page) == limit
    return ids


async def test_keyset_pagination_no_gaps_no_dupes_with_tiebreak(client):
    """≥ limit+1 SMS, часть с РАВНЫМ received_at → tie-break по id.

    Проход всех страниц не пропускает/не дублирует; порядок received_at DESC,
    id DESC; последняя страница → next_cursor=null.
    """
    same_ts = _BASE_TS + timedelta(seconds=10)
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msgp-T1")
            await seed_number(s, phone="+441000090001", team_id=t1)
            expected_rows = []
            # 3 строки с РАЗНЫМ received_at.
            for i in range(3):
                sms = await _seed_sms(
                    s,
                    to_number="+441000090001",
                    team_id=t1,
                    received_at=_BASE_TS + timedelta(seconds=i),
                    sid=f"P-diff-{i}",
                )
                expected_rows.append((sms.received_at, sms.id))
            # 4 строки с ОДИНАКОВЫМ received_at (tie-break по id).
            for i in range(4):
                sms = await _seed_sms(
                    s,
                    to_number="+441000090001",
                    team_id=t1,
                    received_at=same_ts,
                    sid=f"P-same-{i}",
                )
                expected_rows.append((sms.received_at, sms.id))
    cookies, _ = await _admin_auth()

    # Полный отсортированный порядок received_at DESC, id DESC.
    expected_ids = [
        rid
        for _, rid in sorted(expected_rows, key=lambda t: (t[0], t[1]), reverse=True)
    ]
    assert len(expected_ids) == 7

    walked = await _collect_all_ids(client, cookies, limit=2)
    assert walked == expected_ids  # порядок сохранён, без пропусков/дублей
    assert len(walked) == len(set(walked))  # нет дублей


async def test_first_page_next_cursor_present_last_page_null(client):
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msgn-T1")
            await seed_number(s, phone="+441000100001", team_id=t1)
            for i in range(5):
                await _seed_sms(
                    s,
                    to_number="+441000100001",
                    team_id=t1,
                    received_at=_BASE_TS + timedelta(seconds=i),
                    sid=f"N-{i}",
                )
    cookies, _ = await _admin_auth()
    r1 = await client.get("/api/messages", params={"limit": 3}, cookies=cookies)
    assert r1.status_code == 200
    d1 = r1.json()
    assert len(d1["messages"]) == 3
    assert d1["next_cursor"] is not None
    r2 = await client.get(
        "/api/messages",
        params={"limit": 3, "cursor": d1["next_cursor"]},
        cookies=cookies,
    )
    d2 = r2.json()
    assert len(d2["messages"]) == 2
    assert d2["next_cursor"] is None
    # Никаких пересечений между страницами.
    ids1 = {m["id"] for m in d1["messages"]}
    ids2 = {m["id"] for m in d2["messages"]}
    assert ids1.isdisjoint(ids2)


async def test_exactly_limit_rows_no_next_cursor(client):
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msgx-T1")
            await seed_number(s, phone="+441000110001", team_id=t1)
            for i in range(3):
                await _seed_sms(
                    s,
                    to_number="+441000110001",
                    team_id=t1,
                    received_at=_BASE_TS + timedelta(seconds=i),
                    sid=f"X-{i}",
                )
    cookies, _ = await _admin_auth()
    r = await client.get("/api/messages", params={"limit": 3}, cookies=cookies)
    assert r.status_code == 200
    d = r.json()
    assert len(d["messages"]) == 3
    # Ровно limit строк → следующей страницы нет.
    assert d["next_cursor"] is None


async def test_no_cursor_returns_first_page(client):
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msgfp-T1")
            await seed_number(s, phone="+441000120001", team_id=t1)
            newest = await _seed_sms(
                s,
                to_number="+441000120001",
                team_id=t1,
                received_at=_BASE_TS + timedelta(seconds=5),
                sid="FP-new",
            )
            await _seed_sms(
                s,
                to_number="+441000120001",
                team_id=t1,
                received_at=_BASE_TS,
                sid="FP-old",
            )
            newest_id = newest.id
    cookies, _ = await _admin_auth()
    r = await client.get("/api/messages", cookies=cookies)
    assert r.status_code == 200
    # Первая страница начинается с самого свежего (received_at DESC).
    assert r.json()["messages"][0]["id"] == newest_id


# --- §48 Битый курсор --------------------------------------------------------


@pytest.mark.parametrize("bad", ["not-a-real-cursor", "@@@@", "%%%", "zzz zzz"])
async def test_invalid_cursor_400(client, bad):
    cookies, _ = await _admin_auth()
    r = await client.get("/api/messages", params={"cursor": bad}, cookies=cookies)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_cursor"


# --- §49 Невалидный limit ----------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, 101, 1000])
async def test_invalid_limit_400(client, bad):
    cookies, _ = await _admin_auth()
    r = await client.get("/api/messages", params={"limit": bad}, cookies=cookies)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_limit"


@pytest.mark.parametrize("ok", [1, 50, 100])
async def test_limit_boundaries_valid(client, ok):
    cookies, _ = await _admin_auth()
    r = await client.get("/api/messages", params={"limit": ok}, cookies=cookies)
    assert r.status_code == 200


async def test_non_numeric_limit_validation_error_400(client):
    cookies, _ = await _admin_auth()
    r = await client.get("/api/messages", params={"limit": "abc"}, cookies=cookies)
    # Нечисловой limit → FastAPI-валидация (400 validation_error, не invalid_limit).
    assert r.status_code == 400
    assert r.json()["error"] == "validation_error"


async def test_default_limit_is_50(client):
    """limit не передан → дефолт 50 (51-я строка уходит на следующую страницу)."""
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msgd-T1")
            await seed_number(s, phone="+441000130001", team_id=t1)
            for i in range(51):
                await _seed_sms(
                    s,
                    to_number="+441000130001",
                    team_id=t1,
                    received_at=_BASE_TS + timedelta(seconds=i),
                    sid=f"D-{i}",
                )
    cookies, _ = await _admin_auth()
    r = await client.get("/api/messages", cookies=cookies)
    assert r.status_code == 200
    d = r.json()
    assert len(d["messages"]) == 50  # дефолтная страница
    assert d["next_cursor"] is not None  # есть 51-я строка


# --- §50 raw_payload не раскрыт ----------------------------------------------


async def test_raw_payload_not_in_response(client):
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "msgr-T1")
            await seed_number(s, phone="+441000140001", team_id=t1)
            await _seed_sms(
                s,
                to_number="+441000140001",
                team_id=t1,
                received_at=_BASE_TS,
                sid="R-1",
            )
    cookies, _ = await _admin_auth()
    r = await client.get("/api/messages", cookies=cookies)
    assert r.status_code == 200
    msg = r.json()["messages"][0]
    assert set(msg.keys()) == {
        "id",
        "from_number",
        "to_number",
        "body",
        "received_at",
        "team_id",
    }
    assert "raw_payload" not in msg
    assert "must-not-leak" not in r.text


# --- §9 401 без сессии -------------------------------------------------------


async def test_api_messages_requires_session_401(client):
    r = await client.get("/api/messages")
    assert r.status_code == 401
    assert r.json()["error"] == "not_authenticated"


async def test_api_messages_invalid_session_401(client):
    r = await client.get("/api/messages", cookies={"sms_session": "bogus-token-value"})
    assert r.status_code == 401
