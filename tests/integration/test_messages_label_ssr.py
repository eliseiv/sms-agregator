"""Integration: SSR /messages — эффективный лейбл номера (docs/05 §6/§9, Feature 1).

Строка сообщения на номер с label показывает эффективный лейбл (label); на номер
без label — сам to_number. Опция селектора номера показывает label (phone — label).
serialize_message не меняется — карта to_number→label строится в шаблоне из
контекста numbers.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from app.infrastructure.repositories import PhoneNumberRepository, SmsRepository
from shared.db import make_session
from tests.conftest import make_auth, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

_BASE_TS = datetime(2026, 7, 3, 9, 0, 0, tzinfo=UTC)


async def _seed_number(s, *, phone, team_id, label=None):
    return await PhoneNumberRepository(s).create(
        phone_number=phone, team_id=team_id, added_by_user_id=None, label=label
    )


async def _seed_sms(s, *, to_number, team_id, sid):
    return await SmsRepository(s).create(
        twilio_message_sid=sid,
        from_number="+12025550000",
        to_number=to_number,
        body="hello-body",
        team_id=team_id,
        raw_payload={"MessageSid": sid},
        received_at=_BASE_TS,
    )


async def _set_auth(client, user_id, role, team_id):
    cookies, _ = await make_auth(user_id, role, team_id)
    client.cookies.set("sms_session", cookies["sms_session"])


async def test_message_row_shows_effective_label_and_bare_number(client):
    async with make_session() as s:
        async with s.begin():
            t = await seed_team(s, "lblssr-T")
            lead = await seed_user(
                s, username="lblssr-l", role="group_leader", team_id=t
            )
            await _seed_number(s, phone="+441280000001", team_id=t, label="PrimaryLine")
            await _seed_number(s, phone="+441280000002", team_id=t, label=None)
            await _seed_sms(s, to_number="+441280000001", team_id=t, sid="LBL-1")
            await _seed_sms(s, to_number="+441280000002", team_id=t, sid="LBL-2")
            uid = lead.id
    await _set_auth(client, uid, "group_leader", t)
    r = await client.get("/messages")
    assert r.status_code == 200
    body = r.text
    # Номер с label → в строке сообщения показан эффективный лейбл.
    assert "PrimaryLine" in body
    # Строка сообщения на номер с label несёт data-message-to с самим номером
    # (эффективный лейбл — видимый текст; raw-номер — в отдельном span).
    assert 'data-message-to="+441280000001"' in body
    # Номер без label → показан сам to_number (никакого чужого лейбла нет).
    assert 'data-message-to="+441280000002"' in body


async def test_selector_option_shows_label(client):
    async with make_session() as s:
        async with s.begin():
            t = await seed_team(s, "lblssr-sel-T")
            lead = await seed_user(
                s, username="lblssr-sel-l", role="group_leader", team_id=t
            )
            await _seed_number(s, phone="+441280000010", team_id=t, label="NightDesk")
            await _seed_sms(s, to_number="+441280000010", team_id=t, sid="LBL-SEL-1")
            uid = lead.id
    await _set_auth(client, uid, "group_leader", t)
    r = await client.get("/messages")
    assert r.status_code == 200
    # <option> селектора номера показывает "phone — label".
    assert re.search(
        r'<option value="\+441280000010"[^>]*>\s*\+441280000010\s*—\s*NightDesk',
        r.text,
    ), "опция селектора не показывает эффективный лейбл (phone — label)"


async def test_super_admin_message_row_uses_label(client):
    """super_admin видит все номера — эффективный лейбл строится из его набора."""
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="lblssr-root", role="super_admin", team_id=None
            )
            t = await seed_team(s, "lblssr-saT")
            await _seed_number(s, phone="+441280000020", team_id=t, label="OpsLine")
            await _seed_sms(s, to_number="+441280000020", team_id=t, sid="LBL-SA-1")
            uid = admin.id
    await _set_auth(client, uid, "super_admin", None)
    r = await client.get("/messages")
    assert r.status_code == 200
    assert "OpsLine" in r.text
    assert 'data-message-to="+441280000020"' in r.text
