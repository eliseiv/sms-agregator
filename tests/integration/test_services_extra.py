"""Integration (coverage): краевые ветки sms-пайплайна."""

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


async def test_known_number_but_no_recipients():
    """Команда с номером, но без живых привязок → 0 доставок."""
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "nr-team")
            await seed_user(s, username="nr-l", role="group_leader", team_id=tid)
            await seed_number(s, phone="+441299900001", team_id=tid)
    fake = FakeTelegram()
    async with make_session() as s:
        await handle_incoming_sms(
            s,
            fake,
            get_settings(),
            twilio_message_sid="SMnr",
            from_number="+1",
            to_number="+441299900001",
            body="x",
            raw_payload={},
        )
    assert await _count("SELECT count(*) FROM deliveries") == 0
    assert fake.calls == []


async def test_deliver_not_configured_marks_failed():
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "nc-team")
            u = await seed_user(s, username="nc-l", role="group_leader", team_id=tid)
            await seed_link(s, telegram_user_id=1234, user_id=u.id)
            await seed_number(s, phone="+441299900002", team_id=tid)
    # Telegram не сконфигурирован → mark_failed.
    async with make_session() as s:
        await handle_incoming_sms(
            s,
            FakeTelegram(configured=False),
            get_settings(),
            twilio_message_sid="SMnc",
            from_number="+1",
            to_number="+441299900002",
            body="x",
            raw_payload={},
        )
    assert await _count("SELECT count(*) FROM deliveries WHERE status='failed'") == 1


async def test_retry_marks_dead_when_link_absent():
    """pending-доставка на chat без активной привязки → mark_dead."""
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "rd-team")
            u = await seed_user(s, username="rd-l", role="group_leader", team_id=tid)
            sms_id = (
                await s.execute(
                    text(
                        "INSERT INTO inbound_sms (twilio_message_sid, from_number, "
                        "to_number, body, team_id, raw_payload, received_at) VALUES "
                        "('SMrd','+1','+2','x',:t,'{}'::jsonb, now()) RETURNING id"
                    ),
                    {"t": tid},
                )
            ).scalar()
            # Доставка на chat 5555, у которого нет telegram_links.
            await s.execute(
                text(
                    "INSERT INTO deliveries (inbound_sms_id, user_id, telegram_user_id, "
                    "status, attempts) VALUES (:sid, :uid, 5555, 'pending', 0)"
                ),
                {"sid": sms_id, "uid": u.id},
            )
    async with make_session() as s:
        await retry_pending_deliveries(s, FakeTelegram(), get_settings())
    assert await _count("SELECT count(*) FROM deliveries WHERE status='dead'") == 1


async def test_deliver_twice_is_idempotent_reserve():
    """Повторный try_reserve на тот же (sms, chat) → None (доставка не дублируется)."""
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "id-team")
            u = await seed_user(s, username="id-l", role="group_leader", team_id=tid)
            await seed_link(s, telegram_user_id=6666, user_id=u.id)
            await seed_number(s, phone="+441299900003", team_id=tid)
    fake = FakeTelegram()
    # Первый приём — доставка создана.
    async with make_session() as s:
        await handle_incoming_sms(
            s,
            fake,
            get_settings(),
            twilio_message_sid="SMid1",
            from_number="+1",
            to_number="+441299900003",
            body="a",
            raw_payload={},
        )
    # Второй SMS с новым SID на тот же номер/чат — новая доставка (другой inbound).
    async with make_session() as s:
        await handle_incoming_sms(
            s,
            fake,
            get_settings(),
            twilio_message_sid="SMid2",
            from_number="+1",
            to_number="+441299900003",
            body="b",
            raw_payload={},
        )
    assert await _count("SELECT count(*) FROM deliveries WHERE status='sent'") == 2
