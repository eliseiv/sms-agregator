"""Integration: SSR GET /messages — фикс бага no-JS формы фильтра (docs/05 §7/§9).

Баг: no-JS GET-форма фильтра слала пустые строки (`to_number=`, `team_id=`,
`limit=`) для состояний «Все номера»/«Все команды», а строго типизированный
роут ронял их в `422 validation_error`; `to_number=""` фильтровал в пустоту.
Фикс (app/api/routers/messages.py `_clean_str`/`_clean_opt_int`/`_clean_limit`):
пустые/непарсимые query-параметры трактуются как «фильтр не задан», строгая
типизация JSON API `/api/messages` сохранена.

Тестируем ФАКТИЧЕСКИМИ URL, которые генерирует форма (включая дефолтный пустой
submit `?to_number=&team_id=&limit=50`), с проверкой РЕАЛЬНОГО сужения по
контенту (тела сообщений в HTML), а не только наличия 200/рендера. Preselect —
по разметке HTML-ответа. Тела сообщений — уникальные токены, встречаются только
в карточках (`message-card__body`), не в option'ах селектора.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.application.messages_service import DEFAULT_LIMIT
from app.infrastructure.repositories import PhoneNumberRepository, SmsRepository
from shared.db import make_session
from tests.conftest import make_auth, seed_number, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

_BASE_TS = datetime(2026, 7, 3, 15, 0, 0, tzinfo=UTC)


async def _seed_sms(s, *, to_number, team_id, body, received_at, sid=None):
    return await SmsRepository(s).create(
        twilio_message_sid=sid,
        from_number="+12025559999",
        to_number=to_number,
        body=body,
        team_id=team_id,
        raw_payload={"MessageSid": sid or "x"},
        received_at=received_at,
    )


async def _set_auth(client, user_id, role, team_id):
    cookies, _ = await make_auth(user_id, role, team_id)
    client.cookies.set("sms_session", cookies["sms_session"])


def _card_bodies(html: str) -> list[str]:
    """Тела отрисованных карточек SMS (`<p class="message-card__body">…</p>`).

    Наблюдаемый прокси фактического набора видимых сообщений: тело встречается
    ТОЛЬКО в карточке (не в option'ах селектора номеров/команд), поэтому по нему
    можно достоверно судить о сужении фильтром.
    """
    return re.findall(r'class="message-card__body">([^<]*)</p>', html)


def _hidden_limit(html: str) -> int:
    m = re.search(r'name="limit"\s+value="(\d+)"', html)
    assert m is not None, "скрытое поле limit не найдено в форме фильтра"
    return int(m.group(1))


async def _reassign_number(number_id: int, team_id: int | None) -> None:
    async with make_session() as s:
        async with s.begin():
            await PhoneNumberRepository(s).set_team(
                number_id=number_id, team_id=team_id
            )


# ---------------------------------------------------------------------------
# Сценарий 1. super_admin, ДЕФОЛТНЫЙ ПУСТОЙ submit: ?to_number=&team_id=&limit=50
# → 200 (НЕ 422), отрендерены ВСЕ доступные SMS.
# ---------------------------------------------------------------------------


async def test_super_admin_empty_submit_renders_all(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="flt-root1", role="super_admin", team_id=None
            )
            t1 = await seed_team(s, "flt-T1a")
            t2 = await seed_team(s, "flt-T2a")
            await seed_number(s, phone="+441011000001", team_id=t1)
            await seed_number(s, phone="+441011000002", team_id=t2)
            await _seed_sms(
                s,
                to_number="+441011000001",
                team_id=t1,
                body="BODYALPHA1",
                received_at=_BASE_TS,
                sid="F1-a",
            )
            await _seed_sms(
                s,
                to_number="+441011000002",
                team_id=t2,
                body="BODYBETA1",
                received_at=_BASE_TS + timedelta(seconds=1),
                sid="F1-b",
            )
            # SMS на номер вне phone_numbers (удалённый/неизвестный) — super_admin видит.
            await _seed_sms(
                s,
                to_number="+441011000009",
                team_id=None,
                body="BODYGAMMA1",
                received_at=_BASE_TS + timedelta(seconds=2),
                sid="F1-c",
            )
            admin_id = admin.id
    await _set_auth(client, admin_id, "super_admin", None)
    # Фактический URL дефолтной формы: все селекты в состоянии «Все …».
    r = await client.get("/messages?to_number=&team_id=&limit=50")
    assert r.status_code == 200, r.text
    bodies = set(_card_bodies(r.text))
    assert bodies == {"BODYALPHA1", "BODYBETA1", "BODYGAMMA1"}, bodies


# ---------------------------------------------------------------------------
# Сценарий 2. super_admin, конкретный номер → ТОЛЬКО его SMS (сужение по факту).
# ---------------------------------------------------------------------------


async def test_super_admin_filter_by_number_narrows(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="flt-root2", role="super_admin", team_id=None
            )
            t1 = await seed_team(s, "flt-T1b")
            await seed_number(s, phone="+441011010001", team_id=t1)
            await seed_number(s, phone="+441011010002", team_id=t1)
            await _seed_sms(
                s,
                to_number="+441011010001",
                team_id=t1,
                body="BODYALPHA2",
                received_at=_BASE_TS,
                sid="F2-a",
            )
            await _seed_sms(
                s,
                to_number="+441011010002",
                team_id=t1,
                body="BODYBETA2",
                received_at=_BASE_TS + timedelta(seconds=1),
                sid="F2-b",
            )
            admin_id = admin.id
    await _set_auth(client, admin_id, "super_admin", None)
    # Фактический submit формы: httpx кодирует `+` → `%2B` (как браузер).
    r = await client.get(
        "/messages",
        params={"to_number": "+441011010001", "team_id": "", "limit": "50"},
    )
    assert r.status_code == 200, r.text
    bodies = _card_bodies(r.text)
    # Ровно одна карточка — только выбранного номера; чужой номер отсутствует.
    assert bodies == ["BODYALPHA2"], bodies
    assert "BODYBETA2" not in r.text


# ---------------------------------------------------------------------------
# Сценарий 3. super_admin, конкретная команда → сужение до номеров команды
# по ТЕКУЩЕЙ принадлежности (ADR-0014 §2).
# ---------------------------------------------------------------------------


async def test_super_admin_filter_by_team_narrows(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="flt-root3", role="super_admin", team_id=None
            )
            t1 = await seed_team(s, "flt-T1c")
            t2 = await seed_team(s, "flt-T2c")
            await seed_number(s, phone="+441011020001", team_id=t1)
            await seed_number(s, phone="+441011020002", team_id=t2)
            await _seed_sms(
                s,
                to_number="+441011020001",
                team_id=t1,
                body="BODYALPHA3",
                received_at=_BASE_TS,
                sid="F3-a",
            )
            await _seed_sms(
                s,
                to_number="+441011020002",
                team_id=t2,
                body="BODYBETA3",
                received_at=_BASE_TS + timedelta(seconds=1),
                sid="F3-b",
            )
            admin_id, t1_id = admin.id, t1
    await _set_auth(client, admin_id, "super_admin", None)
    r = await client.get(f"/messages?to_number=&team_id={t1_id}&limit=50")
    assert r.status_code == 200, r.text
    bodies = _card_bodies(r.text)
    assert bodies == ["BODYALPHA3"], bodies
    assert "BODYBETA3" not in r.text


async def test_super_admin_filter_by_team_current_ownership(client):
    """Ключевой ADR-0014 §2: фильтр team_id — по ТЕКУЩЕЙ принадлежности номера.

    SMS принят на номер, когда он был в T2 (снимок inbound_sms.team_id=T2).
    Номер переназначен в T1 → фильтр team_id=T1 показывает эту историческую SMS.
    """
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="flt-root3b", role="super_admin", team_id=None
            )
            t1 = await seed_team(s, "flt-T1co")
            t2 = await seed_team(s, "flt-T2co")
            n = await seed_number(s, phone="+441011021001", team_id=t2)
            await _seed_sms(
                s,
                to_number="+441011021001",
                team_id=t2,  # снимок — историческая команда
                body="BODYHIST3",
                received_at=_BASE_TS,
                sid="F3-co",
            )
            admin_id, t1_id, n_id = admin.id, t1, n.id
    await _set_auth(client, admin_id, "super_admin", None)
    # До переназначения: фильтр T1 не показывает SMS (номер в T2).
    r0 = await client.get(f"/messages?to_number=&team_id={t1_id}&limit=50")
    assert r0.status_code == 200
    assert _card_bodies(r0.text) == []
    # Переназначить номер в T1.
    await _reassign_number(n_id, t1_id)
    r1 = await client.get(f"/messages?to_number=&team_id={t1_id}&limit=50")
    assert r1.status_code == 200
    # По текущей принадлежности (T1) историческая SMS видна, несмотря на снимок=T2.
    assert _card_bodies(r1.text) == ["BODYHIST3"], r1.text


# ---------------------------------------------------------------------------
# Сценарий 4. Пустой / битый limit → 200, limit = DEFAULT (50), НЕ 422.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit_qs", ["", "abc", "  ", "12.5", "0x10"])
async def test_empty_or_broken_limit_falls_back_to_default(client, limit_qs):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s,
                username=f"flt-lim-{abs(hash(limit_qs)) % 10000}",
                role="super_admin",
                team_id=None,
            )
            admin_id = admin.id
    await _set_auth(client, admin_id, "super_admin", None)
    r = await client.get(
        "/messages", params={"to_number": "", "team_id": "", "limit": limit_qs}
    )
    assert r.status_code == 200, r.text
    assert _hidden_limit(r.text) == DEFAULT_LIMIT == 50


# ---------------------------------------------------------------------------
# Сценарий 5. Непарсимый team_id → 200, фильтр команды проигнорирован, НЕ 422.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("team_qs", ["abc", "1x", "  ", "null", "1.5"])
async def test_unparseable_team_id_ignored_not_422(client, team_qs):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s,
                username=f"flt-tid-{abs(hash(team_qs)) % 10000}",
                role="super_admin",
                team_id=None,
            )
            t1 = await seed_team(s, "flt-T1e-" + str(abs(hash(team_qs)) % 1000))
            await seed_number(s, phone="+441011030001", team_id=t1)
            await _seed_sms(
                s,
                to_number="+441011030001",
                team_id=t1,
                body="BODYALPHA5",
                received_at=_BASE_TS,
                sid="F5-a",
            )
            admin_id = admin.id
    await _set_auth(client, admin_id, "super_admin", None)
    r = await client.get(
        "/messages", params={"to_number": "", "team_id": team_qs, "limit": "50"}
    )
    # Непарсимый team_id = «фильтр не задан» → 200, видны все SMS (не 422/пусто).
    assert r.status_code == 200, r.text
    assert _card_bodies(r.text) == ["BODYALPHA5"], r.text


# ---------------------------------------------------------------------------
# Сценарий 6. Участник (не super_admin).
# ---------------------------------------------------------------------------


async def _seed_member_with_foreign(s):
    """T1(свой, номер N1/BODYSELF) + T2(чужой, номер N2/BODYFOREIGN); member в T1."""
    t1 = await seed_team(s, "flt-mine6")
    t2 = await seed_team(s, "flt-alien6")
    await seed_user(s, username="flt-l1-6", role="group_leader", team_id=t1)
    member = await seed_user(s, username="flt-m1-6", role="group_member", team_id=t1)
    await seed_user(s, username="flt-l2-6", role="group_leader", team_id=t2)
    await seed_number(s, phone="+441011040001", team_id=t1)
    await seed_number(s, phone="+441011040002", team_id=t2)
    await _seed_sms(
        s,
        to_number="+441011040001",
        team_id=t1,
        body="BODYSELF6",
        received_at=_BASE_TS,
        sid="F6-self",
    )
    await _seed_sms(
        s,
        to_number="+441011040002",
        team_id=t2,
        body="BODYFOREIGN6",
        received_at=_BASE_TS + timedelta(seconds=1),
        sid="F6-alien",
    )
    return member.id, t1, t2


async def test_member_foreign_team_id_ignored_sees_only_own(client):
    async with make_session() as s:
        async with s.begin():
            member_id, t1_id, t2_id = await _seed_member_with_foreign(s)
    await _set_auth(client, member_id, "group_member", t1_id)
    # Участник шлёт team_id чужой команды — игнорируется (для не-super_admin).
    r = await client.get(f"/messages?to_number=&team_id={t2_id}&limit=50")
    assert r.status_code == 200, r.text
    bodies = _card_bodies(r.text)
    assert bodies == ["BODYSELF6"], bodies
    assert "BODYFOREIGN6" not in r.text
    # Селектор команды участнику не рендерится.
    assert 'name="team_id"' not in r.text


async def test_member_foreign_to_number_empty_200_anti_enum(client):
    async with make_session() as s:
        async with s.begin():
            member_id, t1_id, _ = await _seed_member_with_foreign(s)
    await _set_auth(client, member_id, "group_member", t1_id)
    # Запрос чужого номера → пустой список, 200 (не 403/404), существование скрыто.
    r = await client.get(
        "/messages",
        params={"to_number": "+441011040002", "team_id": "", "limit": "50"},
    )
    assert r.status_code == 200, r.text
    assert _card_bodies(r.text) == []
    assert "BODYFOREIGN6" not in r.text
    # Пустой набор → рендерится empty-state, ссылки «Ещё» нет.
    assert "data-messages-more" not in r.text


async def test_member_own_number_narrows(client):
    async with make_session() as s:
        async with s.begin():
            member_id, t1_id, _ = await _seed_member_with_foreign(s)
    await _set_auth(client, member_id, "group_member", t1_id)
    r = await client.get(
        "/messages",
        params={"to_number": "+441011040001", "team_id": "", "limit": "50"},
    )
    assert r.status_code == 200, r.text
    assert _card_bodies(r.text) == ["BODYSELF6"], r.text


async def test_member_empty_submit_renders_own_only(client):
    async with make_session() as s:
        async with s.begin():
            member_id, t1_id, _ = await _seed_member_with_foreign(s)
    await _set_auth(client, member_id, "group_member", t1_id)
    # Дефолтный пустой submit участника: без селектора команды форма шлёт
    # to_number= & limit= (team_id может отсутствовать/быть пустым — оба ок).
    r = await client.get("/messages?to_number=&limit=50")
    assert r.status_code == 200, r.text
    assert _card_bodies(r.text) == ["BODYSELF6"], r.text
    assert "BODYFOREIGN6" not in r.text


# ---------------------------------------------------------------------------
# Сценарий 7. Регресс JSON API — строгая типизация СОХРАНЕНА (в отличие от SSR).
# ---------------------------------------------------------------------------


async def _admin_json_auth():
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="flt-json-root", role="super_admin", team_id=None
            )
    return await make_auth(admin.id, "super_admin", None)


async def test_json_api_limit_abc_400_validation_error(client):
    cookies, _ = await _admin_json_auth()
    r = await client.get("/api/messages", params={"limit": "abc"}, cookies=cookies)
    # JSON API остаётся строго типизированным: непарсимый limit → 400.
    assert r.status_code == 400
    assert r.json()["error"] == "validation_error"


async def test_json_api_empty_limit_400_validation_error(client):
    cookies, _ = await _admin_json_auth()
    # Строгая типизация JSON API: пустая строка не приводится к int → 400.
    # (JS-клиент пустой limit не шлёт — контракт; фиксируем фактическое поведение.)
    r = await client.get("/api/messages?limit=", cookies=cookies)
    assert r.status_code == 400
    assert r.json()["error"] == "validation_error"


async def test_json_api_empty_team_id_400_validation_error(client):
    cookies, _ = await _admin_json_auth()
    # Строгая типизация сохранена: пустой team_id на JSON API → 400 (в отличие от
    # SSR, где пустое = «не задан»). JS-клиент пустой team_id не шлёт (контракт).
    r = await client.get("/api/messages?team_id=", cookies=cookies)
    assert r.status_code == 400
    assert r.json()["error"] == "validation_error"


async def test_json_api_int_team_id_ok(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="flt-json-tid", role="super_admin", team_id=None
            )
            t1 = await seed_team(s, "flt-json-T1")
            await seed_number(s, phone="+441011050001", team_id=t1)
            await _seed_sms(
                s,
                to_number="+441011050001",
                team_id=t1,
                body="BODYJSON7",
                received_at=_BASE_TS,
                sid="F7-a",
            )
            admin_id, t1_id = admin.id, t1
    cookies, _ = await make_auth(admin_id, "super_admin", None)
    r = await client.get("/api/messages", params={"team_id": t1_id}, cookies=cookies)
    assert r.status_code == 200
    assert {m["to_number"] for m in r.json()["messages"]} == {"+441011050001"}


# ---------------------------------------------------------------------------
# Сценарий 8. Preselect: активный фильтр отражён в <option ... selected>.
# ---------------------------------------------------------------------------


async def test_preselect_number_and_team_via_form_url(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="flt-pre8", role="super_admin", team_id=None
            )
            t1 = await seed_team(s, "flt-T1pre8")
            await seed_number(s, phone="+441011060001", team_id=t1)
            await _seed_sms(
                s,
                to_number="+441011060001",
                team_id=t1,
                body="BODYPRE8",
                received_at=_BASE_TS,
                sid="F8-a",
            )
            admin_id, t1_id = admin.id, t1
    await _set_auth(client, admin_id, "super_admin", None)
    r = await client.get(
        "/messages",
        params={"to_number": "+441011060001", "team_id": str(t1_id), "limit": "50"},
    )
    assert r.status_code == 200, r.text
    # Номер preselect'нут.
    assert re.search(
        r'<option value="\+441011060001"[^>]*\bselected\b', r.text
    ), "to_number не preselect'нут"
    # Команда preselect'нута.
    assert re.search(
        rf'<option value="{t1_id}"[^>]*\bselected\b', r.text
    ), "team_id не preselect'нут"
