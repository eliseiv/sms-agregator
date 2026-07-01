"""Integration: crash-recoverable fan-out (docs/06 §9a; ADR-0005 §4).

Симулируем частичное состояние (доставлен только U1) + повтор webhook по тому же
MessageSid → U2 добирается, U1 не дублируется. Также retry-путь pending→sent.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.application.services import handle_incoming_sms, retry_pending_deliveries
from shared.config import get_settings
from shared.db import make_session
from tests.conftest import FakeTelegram, seed_link, seed_number, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _count(sql, **p):
    async with make_session() as s:
        return int((await s.execute(text(sql), p)).scalar() or 0)


async def _seed_two_recipient_team(phone: str, sid: str):
    """team + 2 живых получателя + номер + частичное состояние (U1 доставлен)."""
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "cr-" + sid)
            u1 = await seed_user(s, username=sid + "-u1", role="group_leader", team_id=tid)
            u2 = await seed_user(s, username=sid + "-u2", role="group_member", team_id=tid)
            await seed_link(s, telegram_user_id=1110, user_id=u1.id)
            await seed_link(s, telegram_user_id=2220, user_id=u2.id)
            await seed_number(s, phone=phone, team_id=tid)
            # Частичное состояние: inbound_sms сохранён + только U1 доставлен.
            sms_id = (
                await s.execute(
                    text(
                        "INSERT INTO inbound_sms (twilio_message_sid, from_number, "
                        "to_number, body, team_id, raw_payload, received_at) VALUES "
                        "(:sid,'+1',:to,'hi',:t,'{}'::jsonb, now()) RETURNING id"
                    ),
                    {"sid": sid, "to": phone, "t": tid},
                )
            ).scalar()
            await s.execute(
                text(
                    "INSERT INTO deliveries (inbound_sms_id, user_id, telegram_user_id, "
                    "status, attempts, sent_at) VALUES (:s, :u, 1110, 'sent', 1, now())"
                ),
                {"s": sms_id, "u": u1.id},
            )
        return tid, u1.id, u2.id, sms_id


async def test_9a_webhook_retry_completes_fanout_without_dup():
    tid, u1, u2, sms_id = await _seed_two_recipient_team("+441277700001", "SM9a")
    fake = FakeTelegram()
    # Повтор webhook с тем же MessageSid → дедуп-ветка → общий fan-out.
    async with make_session() as s:
        await handle_incoming_sms(
            s, fake, get_settings(),
            twilio_message_sid="SM9a", from_number="+1",
            to_number="+441277700001", body="hi", raw_payload={"MessageSid": "SM9a"},
        )
    # inbound_sms не задублирован.
    assert await _count("SELECT count(*) FROM inbound_sms WHERE twilio_message_sid='SM9a'") == 1
    # Обе доставки sent, без дублей.
    assert await _count("SELECT count(*) FROM deliveries WHERE inbound_sms_id=:s", s=sms_id) == 2
    assert await _count("SELECT count(*) FROM deliveries WHERE inbound_sms_id=:s AND status='sent'", s=sms_id) == 2
    # U1 не задублирован (одна строка).
    assert await _count("SELECT count(*) FROM deliveries WHERE inbound_sms_id=:s AND user_id=:u", s=sms_id, u=u1) == 1
    # Отправлено только U2 (chat 2220); U1 повторно не слали.
    assert fake.calls == [(2220, fake.calls[0][1])]
    assert fake.calls[0][0] == 2220


async def test_9a_retry_loop_completes_pending():
    """Ветка через retry_pending_deliveries: pending U2 → sent."""
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "cr-retry")
            u1 = await seed_user(s, username="crr-u1", role="group_leader", team_id=tid)
            u2 = await seed_user(s, username="crr-u2", role="group_member", team_id=tid)
            await seed_link(s, telegram_user_id=3330, user_id=u1.id)
            await seed_link(s, telegram_user_id=4440, user_id=u2.id)
            sms_id = (
                await s.execute(
                    text(
                        "INSERT INTO inbound_sms (twilio_message_sid, from_number, "
                        "to_number, body, team_id, raw_payload, received_at) VALUES "
                        "('SM9b','+1','+441277700002','hi',:t,'{}'::jsonb, now()) RETURNING id"
                    ),
                    {"t": tid},
                )
            ).scalar()
            # U1 sent, U2 остался pending (крэш до отправки).
            await s.execute(
                text(
                    "INSERT INTO deliveries (inbound_sms_id, user_id, telegram_user_id, status, attempts, sent_at) "
                    "VALUES (:s, :u, 3330, 'sent', 1, now())"
                ),
                {"s": sms_id, "u": u1.id},
            )
            await s.execute(
                text(
                    "INSERT INTO deliveries (inbound_sms_id, user_id, telegram_user_id, status, attempts) "
                    "VALUES (:s, :u, 4440, 'pending', 0)"
                ),
                {"s": sms_id, "u": u2.id},
            )
    fake = FakeTelegram()
    async with make_session() as s:
        retried = await retry_pending_deliveries(s, fake, get_settings())
    assert retried == 1
    assert await _count("SELECT count(*) FROM deliveries WHERE inbound_sms_id=:s AND status='sent'", s=sms_id) == 2
    assert fake.calls == [(4440, fake.calls[0][1])]
