"""Integration: наличие CHECK/UNIQUE/DEFERRABLE FK/триггеров + их работа (docs/06 §Схема)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from shared.db import make_session

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _scalar(session, sql: str, **params):
    return (await session.execute(text(sql), params)).scalar()


async def test_check_constraints_exist():
    async with make_session() as s:
        names = {
            r[0]
            for r in (
                await s.execute(
                    text("SELECT conname FROM pg_constraint WHERE contype='c'")
                )
            ).all()
        }
    for expected in (
        "ck_users_role_team_invariant",
        "ck_users_username_lower",
        "ck_users_role",
        "ck_deliveries_status",
        "ck_teams_name_length",
    ):
        assert expected in names, f"missing CHECK {expected}"


async def test_partial_unique_indexes_exist():
    async with make_session() as s:
        idx = {
            r[0]
            for r in (
                await s.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname='public'")
                )
            ).all()
        }
    for expected in (
        "users_single_super_admin",
        "inbound_sms_sid_uq",
        "uq_teams_leader_user_id",
    ):
        assert expected in idx, f"missing index {expected}"


async def test_unique_constraints_exist():
    async with make_session() as s:
        names = {
            r[0]
            for r in (
                await s.execute(
                    text("SELECT conname FROM pg_constraint WHERE contype='u'")
                )
            ).all()
        }
    assert "deliveries_sms_chat_uq" in names
    assert "uq_phone_numbers_phone_number" in names


async def test_deferrable_fk_users_team_id():
    async with make_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT condeferrable, condeferred FROM pg_constraint "
                    "WHERE conname='fk_users_team_id'"
                )
            )
        ).one()
    assert row[0] is True  # DEFERRABLE
    assert row[1] is True  # INITIALLY DEFERRED


async def test_triggers_exist():
    async with make_session() as s:
        trigs = {
            r[0]
            for r in (
                await s.execute(
                    text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
                )
            ).all()
        }
    assert "trg_users_updated_at" in trigs
    assert "trg_users_team_leader_consistency" in trigs


async def test_updated_at_trigger_fires():
    async with make_session() as s:
        async with s.begin():
            await s.execute(text("INSERT INTO teams (name) VALUES ('trg-team')"))
    async with make_session() as s:
        async with s.begin():
            await s.execute(
                text("UPDATE teams SET name='trg-team-2' WHERE name='trg-team'")
            )
    async with make_session() as s:
        recent = await _scalar(
            s,
            "SELECT (now() - updated_at) < interval '30 seconds' "
            "FROM teams WHERE name='trg-team-2'",
        )
    assert recent is True


async def test_role_team_invariant_rejected():
    # group_member без team_id нарушает CHECK.
    async with make_session() as s:
        with pytest.raises(IntegrityError):
            async with s.begin():
                await s.execute(
                    text(
                        "INSERT INTO users (username, role, team_id) "
                        "VALUES ('bad', 'group_member', NULL)"
                    )
                )


async def test_single_super_admin_partial_unique():
    async with make_session() as s:
        async with s.begin():
            await s.execute(
                text(
                    "INSERT INTO users (username, role, team_id) "
                    "VALUES ('sa1', 'super_admin', NULL)"
                )
            )
        with pytest.raises(IntegrityError):
            async with s.begin():
                await s.execute(
                    text(
                        "INSERT INTO users (username, role, team_id) "
                        "VALUES ('sa2', 'super_admin', NULL)"
                    )
                )


async def test_username_lower_check():
    async with make_session() as s:
        async with s.begin():
            await s.execute(text("INSERT INTO teams (id, name) VALUES (900, 'lc')"))
        with pytest.raises(IntegrityError):
            async with s.begin():
                await s.execute(
                    text(
                        "INSERT INTO users (username, role, team_id) "
                        "VALUES ('MixedCase', 'group_member', 900)"
                    )
                )
