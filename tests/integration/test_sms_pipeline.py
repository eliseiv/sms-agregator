"""Integration: приём SMS и доставка (docs/06 §Приём SMS 4-9).

Реальная БД. Telegram Bot API — FakeTelegram. handle_incoming_sms вызывается
напрямую (сервисный уровень); webhook-контракт проверяется отдельно.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.application.services import handle_incoming_sms, retry_pending_deliveries
from app.infrastructure.telegram_api import TelegramApiError, TelegramForbiddenError
from shared.config import get_settings
from shared.db import make_session
from tests.conftest import FakeTelegram, seed_link, seed_number, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _count(session, sql: str, **p) -> int:
    return int((await session.execute(text(sql), p)).scalar() or 0)


async def _seed_team_with_recipient(
    *, phone: str, tg_id: int, username: str = "leader"
) -> tuple[int, int, int]:
    """team_id, user_id, tg_id. Один участник с живой привязкой + номер."""
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "T-" + phone[-4:])
            u = await seed_user(s, username=username, role="group_leader", team_id=tid)
            await seed_link(s, telegram_user_id=tg_id, user_id=u.id)
            await seed_number(s, phone=phone, team_id=tid, added_by=u.id)
        return tid, u.id, tg_id


async def test_receive_sms_creates_inbound_and_delivery():
    tid, uid, tg = await _seed_team_with_recipient(phone="+441234500001", tg_id=5001)
    fake = FakeTelegram()
    async with make_session() as s:
        sms = await handle_incoming_sms(
            s,
            fake,
            get_settings(),
            twilio_message_sid="SMt1",
            from_number="+12025550001",
            to_number="+441234500001",
            body="hi",
            raw_payload={"MessageSid": "SMt1"},
        )
    assert sms.team_id == tid
    async with make_session() as s:
        assert await _count(s, "SELECT count(*) FROM inbound_sms") == 1
        assert (
            await _count(s, "SELECT count(*) FROM deliveries WHERE status='sent'") == 1
        )
    assert fake.calls and fake.calls[0][0] == tg
    assert "hi" in fake.calls[0][1]


async def test_multi_recipient_two_deliveries():
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "Tmulti")
            u1 = await seed_user(s, username="m-lead", role="group_leader", team_id=tid)
            u2 = await seed_user(s, username="m-two", role="group_member", team_id=tid)
            await seed_link(s, telegram_user_id=6001, user_id=u1.id)
            await seed_link(s, telegram_user_id=6002, user_id=u2.id)
            await seed_number(s, phone="+441234500002", team_id=tid)
    fake = FakeTelegram()
    async with make_session() as s:
        await handle_incoming_sms(
            s,
            fake,
            get_settings(),
            twilio_message_sid="SMt2",
            from_number="+12025550002",
            to_number="+441234500002",
            body="hey",
            raw_payload={},
        )
    async with make_session() as s:
        assert (
            await _count(s, "SELECT count(*) FROM deliveries WHERE status='sent'") == 2
        )
    assert {c[0] for c in fake.calls} == {6001, 6002}


async def test_idempotent_same_sid():
    await _seed_team_with_recipient(phone="+441234500003", tg_id=7001)
    fake = FakeTelegram()
    for _ in range(2):
        async with make_session() as s:
            await handle_incoming_sms(
                s,
                fake,
                get_settings(),
                twilio_message_sid="SMdup",
                from_number="+12025550003",
                to_number="+441234500003",
                body="dup",
                raw_payload={},
            )
    async with make_session() as s:
        assert await _count(s, "SELECT count(*) FROM inbound_sms") == 1
        assert await _count(s, "SELECT count(*) FROM deliveries") == 1
    # Второй прогон вернул существующий SMS без новой доставки.
    assert len(fake.calls) == 1


async def test_unknown_number_no_delivery():
    fake = FakeTelegram()
    async with make_session() as s:
        sms = await handle_incoming_sms(
            s,
            fake,
            get_settings(),
            twilio_message_sid="SMunk",
            from_number="+12025550004",
            to_number="+449999999999",
            body="who",
            raw_payload={},
        )
    assert sms.team_id is None
    async with make_session() as s:
        assert (
            await _count(s, "SELECT count(*) FROM inbound_sms WHERE team_id IS NULL")
            == 1
        )
        assert await _count(s, "SELECT count(*) FROM deliveries") == 0
    assert fake.calls == []


async def test_forbidden_marks_delivery_dead_and_link_dead():
    _, _, tg = await _seed_team_with_recipient(phone="+441234500005", tg_id=8001)

    def boom(chat_id, text_):
        raise TelegramForbiddenError("bot was blocked")

    fake = FakeTelegram(behavior=boom)
    async with make_session() as s:
        await handle_incoming_sms(
            s,
            fake,
            get_settings(),
            twilio_message_sid="SMforb",
            from_number="+12025550005",
            to_number="+441234500005",
            body="x",
            raw_payload={},
        )
    async with make_session() as s:
        assert (
            await _count(s, "SELECT count(*) FROM deliveries WHERE status='dead'") == 1
        )
        assert (
            await _count(
                s, "SELECT count(*) FROM telegram_links WHERE dead_at IS NOT NULL"
            )
            == 1
        )
    # retry не должен брать dead-доставку.
    async with make_session() as s:
        retried = await retry_pending_deliveries(s, FakeTelegram(), get_settings())
    assert retried == 0


async def test_retry_failed_then_success():
    _, _, tg = await _seed_team_with_recipient(phone="+441234500006", tg_id=9001)

    calls = {"n": 0}

    def transient(chat_id, text_):
        calls["n"] += 1
        raise TelegramApiError("temporary 500")

    async with make_session() as s:
        await handle_incoming_sms(
            s,
            FakeTelegram(behavior=transient),
            get_settings(),
            twilio_message_sid="SMretry",
            from_number="+12025550006",
            to_number="+441234500006",
            body="retry",
            raw_payload={},
        )
    async with make_session() as s:
        assert (
            await _count(s, "SELECT count(*) FROM deliveries WHERE status='failed'")
            == 1
        )

    # Повторная отправка — теперь успех.
    ok_fake = FakeTelegram()
    async with make_session() as s:
        retried = await retry_pending_deliveries(s, ok_fake, get_settings())
    assert retried == 1
    async with make_session() as s:
        assert (
            await _count(s, "SELECT count(*) FROM deliveries WHERE status='sent'") == 1
        )
    assert ok_fake.calls and ok_fake.calls[0][0] == 9001


async def test_retry_skips_when_attempts_exhausted():
    _, uid, tg = await _seed_team_with_recipient(phone="+441234500007", tg_id=9101)
    # Вставим доставку с attempts >= max вручную.
    settings = get_settings()
    async with make_session() as s:
        async with s.begin():
            sms_id = (
                await s.execute(
                    text(
                        "INSERT INTO inbound_sms (twilio_message_sid, from_number, "
                        "to_number, body, team_id, raw_payload, received_at) VALUES "
                        "('SMex','+1','+441234500007','x',"
                        "(SELECT team_id FROM phone_numbers WHERE phone_number='+441234500007'),"
                        "'{}'::jsonb, now()) RETURNING id"
                    )
                )
            ).scalar()
            await s.execute(
                text(
                    "INSERT INTO deliveries (inbound_sms_id, user_id, telegram_user_id, "
                    "status, attempts) VALUES (:sid, :uid, :tg, 'failed', :att)"
                ),
                {
                    "sid": sms_id,
                    "uid": uid,
                    "tg": 9101,
                    "att": settings.DELIVERY_MAX_ATTEMPTS,
                },
            )
    fake = FakeTelegram()
    async with make_session() as s:
        retried = await retry_pending_deliveries(s, fake, settings)
    assert retried == 0
    assert fake.calls == []


async def test_webhook_endpoint_contract(client, monkeypatch):
    """POST webhook → 200 + <Response></Response>; inbound_sms создан."""
    await _seed_team_with_recipient(phone="+441234500008", tg_id=9201)
    # Патчим telegram-клиент вебхука на фейк, чтобы доставка не ходила в сеть.
    import app.api.routers.webhooks as wh

    fake = FakeTelegram()
    monkeypatch.setattr(wh, "get_telegram_client", lambda settings: fake)

    resp = await client.post(
        "/api/webhooks/twilio/sms",
        content="MessageSid=SMhttp&From=%2B12025550008&To=%2B441234500008&Body=hello",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    assert resp.text == "<Response></Response>"
    async with make_session() as s:
        assert (
            await _count(
                s, "SELECT count(*) FROM inbound_sms WHERE twilio_message_sid='SMhttp'"
            )
            == 1
        )
    assert fake.calls and fake.calls[0][0] == 9201
