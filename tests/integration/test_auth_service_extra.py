"""Integration (coverage): AuthService.lookup_for_login, set-password, locked helper."""

from __future__ import annotations

import pytest

from app.application.auth_service import AuthService, raise_locked_if_needed
from app.core.security import hash_password
from app.exceptions import AccountLockedError, NotAuthenticatedError
from shared.db import make_session
from tests.conftest import seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_lookup_for_login_not_found():
    async with make_session() as s:
        res = await AuthService(s).lookup_for_login(username="ghost")
    assert res.kind == "not_found"


async def test_lookup_for_login_ready_and_set_password():
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "lk-team")
            await seed_user(
                s, username="ready", role="group_leader", team_id=tid,
                password_hash=hash_password("pw12345678"),
            )
            await seed_user(
                s, username="needspw", role="group_member", team_id=tid,
                password_reset_required=True,
            )
    async with make_session() as s:
        async with s.begin():
            r_ready = await AuthService(s).lookup_for_login(username="ready")
    assert r_ready.kind == "ready_for_password"
    async with make_session() as s:
        async with s.begin():
            r_set = await AuthService(s).lookup_for_login(username="needspw")
    assert r_set.kind == "set_password_required"
    assert r_set.setup_token is not None


async def test_complete_set_password_invalid_token_raises():
    async with make_session() as s:
        with pytest.raises(NotAuthenticatedError):
            await AuthService(s).complete_set_password(
                setup_token="does-not-exist", password="whatever12", ip="1.1.1.1",
                user_agent="t",
            )


def test_raise_locked_if_needed():
    with pytest.raises(AccountLockedError):
        raise_locked_if_needed(30)
    # None → без исключения.
    raise_locked_if_needed(None)


async def test_login_invalid_for_unknown_user_timing_parity():
    """Неизвестный логин → invalid (verify против DUMMY_HASH, без утечки)."""
    async with make_session() as s:
        async with s.begin():
            res = await AuthService(s).login(
                username="no-such-user", password="x", ip="1.1.1.1", user_agent="t"
            )
    assert res.kind == "invalid"
