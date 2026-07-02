"""Integration: миграция 002 — team_id NULLABLE + FK ON DELETE SET NULL (docs/06 §1)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.application.services import handle_incoming_sms
from shared.config import get_settings
from shared.db import make_session
from tests.conftest import FakeTelegram

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_phone_numbers_team_id_is_nullable():
    async with make_session() as s:
        nullable = (
            await s.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name='phone_numbers' AND column_name='team_id'"
                )
            )
        ).scalar()
    assert nullable == "YES"


async def test_fk_on_delete_set_null():
    async with make_session() as s:
        action = (
            await s.execute(
                text(
                    "SELECT confdeltype::text FROM pg_constraint "
                    "WHERE conname='fk_phone_numbers_team_id'"
                )
            )
        ).scalar()
    # 'n' = SET NULL (было 'c' = CASCADE).
    assert action == "n"


async def test_delete_team_unassigns_numbers_not_deletes():
    async with make_session() as s:
        async with s.begin():
            tid = (
                await s.execute(
                    text("INSERT INTO teams (name) VALUES ('del-t') RETURNING id")
                )
            ).scalar()
            num_id = (
                await s.execute(
                    text(
                        "INSERT INTO phone_numbers (phone_number, team_id, is_active) "
                        "VALUES ('+441260000001', :t, true) RETURNING id"
                    ),
                    {"t": tid},
                )
            ).scalar()
        # Удаляем команду (пустую, без лидера/участников).
        async with s.begin():
            await s.execute(text("DELETE FROM teams WHERE id=:t"), {"t": tid})
    async with make_session() as s:
        row = (
            await s.execute(
                text("SELECT team_id FROM phone_numbers WHERE id=:i"), {"i": num_id}
            )
        ).one_or_none()
    assert row is not None  # номер НЕ удалён
    assert row[0] is None  # стал unassigned


async def test_sms_to_unassigned_number_no_recipients():
    """SMS на номер team_id NULL → inbound_sms сохранён, 0 доставок."""
    async with make_session() as s:
        async with s.begin():
            # unassigned номер (team_id NULL).
            await s.execute(
                text(
                    "INSERT INTO phone_numbers (phone_number, team_id, is_active) "
                    "VALUES ('+441260000002', NULL, true)"
                )
            )
    fake = FakeTelegram()
    async with make_session() as s:
        sms = await handle_incoming_sms(
            s,
            fake,
            get_settings(),
            twilio_message_sid="SMunassigned",
            from_number="+1",
            to_number="+441260000002",
            body="hi",
            raw_payload={},
        )
    assert sms.team_id is None
    async with make_session() as s:
        inbound = (
            await s.execute(
                text(
                    "SELECT count(*) FROM inbound_sms WHERE twilio_message_sid='SMunassigned'"
                )
            )
        ).scalar()
        deliveries = (await s.execute(text("SELECT count(*) FROM deliveries"))).scalar()
    assert inbound == 1
    assert deliveries == 0
    assert fake.calls == []
