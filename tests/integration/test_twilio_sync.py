"""Integration: on-demand Twilio-sync (ADR-0013, docs/06 §26a–26f). Twilio замокан."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.application.twilio_sync_service import sync_twilio_numbers_to_pool
from app.infrastructure.twilio_numbers import TwilioNumber, TwilioNumbersApiError
from shared.db import make_session
from tests.conftest import make_auth, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SYNC_URL = "/api/admin/numbers/sync"


class FakeTwilioClient:
    """Мок TwilioNumbersClient: управляемый набор номеров / ошибка / конфиг."""

    def __init__(self, numbers=None, *, configured=True, raise_api=False):
        self._numbers = numbers or []
        self._configured = configured
        self._raise = raise_api
        self.calls = 0

    @property
    def is_configured(self) -> bool:
        return self._configured

    def list_incoming_numbers(self):
        self.calls += 1
        if self._raise:
            raise TwilioNumbersApiError("simulated Twilio timeout")
        return list(self._numbers)


def _nums(specs):
    return [TwilioNumber(phone_number=p, friendly_name=fn) for p, fn in specs]


async def _super_admin(name="ts-root"):
    async with make_session() as s:
        async with s.begin():
            u = await seed_user(s, username=name, role="super_admin", team_id=None)
        return u.id


async def _count(sql, **p):
    async with make_session() as s:
        return int((await s.execute(text(sql), p)).scalar() or 0)


def _patch_client(monkeypatch, client):
    import app.api.routers.admin_numbers as an

    monkeypatch.setattr(an, "get_twilio_numbers_client", lambda *a, **k: client)


# --- §26a: базовый success --------------------------------------------------


async def test_sync_success_counts(client, monkeypatch):
    admin = await _super_admin("ts-a")
    fake = FakeTwilioClient(_nums([("+12025550001", "One"), ("+12025550002", "Two")]))
    _patch_client(monkeypatch, fake)
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.post(_SYNC_URL, cookies=cookies, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["synced_total"] == 2
    assert body["added"] == 2
    assert body["skipped_existing"] == 0
    assert await _count("SELECT count(*) FROM phone_numbers WHERE team_id IS NULL") == 2


# --- §26b: пагинация (несколько страниц) ------------------------------------


async def test_sync_pagination_all_pages(client, monkeypatch):
    admin = await _super_admin("ts-pg")
    # >100 номеров = несколько страниц Twilio; SDK .list() их обходит.
    specs = [(f"+1202600{i:04d}", f"N{i}") for i in range(120)]
    fake = FakeTwilioClient(_nums(specs))
    _patch_client(monkeypatch, fake)
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.post(_SYNC_URL, cookies=cookies, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["synced_total"] == 120
    assert body["added"] == 120
    assert await _count("SELECT count(*) FROM phone_numbers") == 120


# --- §26c: идемпотентность + не трогает назначенные -------------------------


async def test_sync_idempotent_and_preserves_assigned(client, monkeypatch):
    admin = await _super_admin("ts-idem")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "ts-team")
            leader = await seed_user(
                s, username="ts-l", role="group_leader", team_id=tid
            )
            # Номер уже назначен команде (team_id/label/added_by заданы).
            await s.execute(
                text(
                    "INSERT INTO phone_numbers (phone_number, team_id, added_by_user_id, label, is_active) "
                    "VALUES ('+12025550001', :t, :u, 'assigned-label', true)"
                ),
                {"t": tid, "u": leader.id},
            )
    fake = FakeTwilioClient(
        _nums([("+12025550001", "Twilio Name"), ("+12025550009", "New")])
    )
    _patch_client(monkeypatch, fake)
    cookies, headers = await make_auth(admin, "super_admin", None)

    r1 = await client.post(_SYNC_URL, cookies=cookies, headers=headers)
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["synced_total"] == 2
    assert b1["added"] == 1  # только новый +...09
    assert b1["skipped_existing"] == 1

    # Назначенный номер НЕ изменён (team_id/label/added_by).
    async with make_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT team_id, label, added_by_user_id FROM phone_numbers "
                    "WHERE phone_number='+12025550001'"
                )
            )
        ).one()
    assert row[0] == tid
    assert row[1] == "assigned-label"
    assert row[2] == leader.id

    # Повторный sync → added=0.
    r2 = await client.post(_SYNC_URL, cookies=cookies, headers=headers)
    assert r2.json()["added"] == 0
    assert r2.json()["skipped_existing"] == 2
    assert await _count("SELECT count(*) FROM phone_numbers") == 2


# --- §26d: нормализация + дедуп внутри батча --------------------------------


async def test_sync_normalizes_and_dedupes(client, monkeypatch):
    admin = await _super_admin("ts-norm")
    fake = FakeTwilioClient(
        _nums(
            [
                ("+1 (202) 555-0001", "Main"),  # → +12025550001
                ("12025550001", "Dup"),  # дубль после нормализации
                ("+12025550002", "Two"),
            ]
        )
    )
    _patch_client(monkeypatch, fake)
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.post(_SYNC_URL, cookies=cookies, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["synced_total"] == 3
    assert body["added"] == 2  # дубль внутри батча отброшен
    assert body["skipped_existing"] == 1
    async with make_session() as s:
        rows = {
            r[0]: r[1]
            for r in (
                await s.execute(text("SELECT phone_number, label FROM phone_numbers"))
            ).all()
        }
    assert set(rows) == {"+12025550001", "+12025550002"}
    assert rows["+12025550001"] == "Main"  # label=friendly_name, первый выигрывает
    assert rows["+12025550002"] == "Two"


# --- §26e: ошибки конфигурации / API ----------------------------------------


async def test_sync_twilio_not_configured_503(client):
    """Пустые TWILIO_ACCOUNT_SID/AUTH_TOKEN (дефолт conftest) → 503."""
    admin = await _super_admin("ts-503")
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.post(_SYNC_URL, cookies=cookies, headers=headers)
    assert r.status_code == 503
    assert r.json()["error"] == "twilio_not_configured"


async def test_sync_twilio_error_502(client, monkeypatch):
    admin = await _super_admin("ts-502")
    fake = FakeTwilioClient(raise_api=True)
    _patch_client(monkeypatch, fake)
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.post(_SYNC_URL, cookies=cookies, headers=headers)
    assert r.status_code == 502
    assert r.json()["error"] == "twilio_error"


# --- §26f: auth / CSRF / audit ----------------------------------------------


async def test_sync_forbidden_for_member(client, monkeypatch):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "ts-fb")
            await seed_user(s, username="ts-fbl", role="group_leader", team_id=tid)
            m = await seed_user(s, username="ts-fbm", role="group_member", team_id=tid)
    _patch_client(monkeypatch, FakeTwilioClient(_nums([("+12025550001", "x")])))
    cookies, headers = await make_auth(m.id, "group_member", tid)
    r = await client.post(_SYNC_URL, cookies=cookies, headers=headers)
    assert r.status_code == 403


async def test_sync_forbidden_for_leader(client, monkeypatch):
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "ts-fbl2")
            leader = await seed_user(
                s, username="ts-fbll", role="group_leader", team_id=tid
            )
    _patch_client(monkeypatch, FakeTwilioClient(_nums([("+12025550001", "x")])))
    cookies, headers = await make_auth(leader.id, "group_leader", tid)
    r = await client.post(_SYNC_URL, cookies=cookies, headers=headers)
    assert r.status_code == 403


async def test_sync_requires_csrf(client):
    admin = await _super_admin("ts-csrf")
    cookies, _ = await make_auth(admin, "super_admin", None)
    # без X-CSRF-Token → CSRF-gate 403 (fail-closed).
    r = await client.post(_SYNC_URL, cookies=cookies)
    assert r.status_code == 403
    assert r.json()["error"] == "csrf_failed"


async def test_sync_anonymous_rejected(client):
    # Без сессии/CSRF — эндпоинт защищён (fail-closed).
    r = await client.post(_SYNC_URL)
    assert r.status_code in (401, 403)


async def test_sync_writes_audit(client, monkeypatch):
    admin = await _super_admin("ts-audit")
    fake = FakeTwilioClient(_nums([("+12025550001", "A"), ("+12025550002", "B")]))
    _patch_client(monkeypatch, fake)
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.post(_SYNC_URL, cookies=cookies, headers=headers)
    assert r.status_code == 200
    async with make_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT details FROM admin_audit WHERE action='numbers_synced' "
                    "AND actor_user_id=:a"
                ),
                {"a": admin},
            )
        ).one_or_none()
    assert row is not None
    details = row[0]
    assert details["synced_total"] == 2
    assert details["added"] == 2
    assert details["skipped_existing"] == 0


# --- CLI-паритет (общий сервис) ---------------------------------------------


async def test_shared_service_cli_parity_idempotent():
    """scripts CLI использует sync_twilio_numbers_to_pool — те же счётчики/идемпотентность."""
    fake = FakeTwilioClient(_nums([("+12025557001", "P1"), ("+1 202 555 7002", "P2")]))
    async with make_session() as s:
        r1 = await sync_twilio_numbers_to_pool(s, fake)
    assert r1.synced_total == 2
    assert r1.added == 2
    assert r1.skipped_existing == 0
    # Повтор — без дублей (added=0), как в CLI.
    async with make_session() as s:
        r2 = await sync_twilio_numbers_to_pool(s, fake)
    assert r2.added == 0
    assert r2.skipped_existing == 2
    assert (
        await _count(
            "SELECT count(*) FROM phone_numbers WHERE phone_number LIKE '+1202555700%'"
        )
        == 2
    )


async def test_shared_service_not_configured_raises():
    from app.infrastructure.twilio_numbers import TwilioNotConfiguredError

    fake = FakeTwilioClient(configured=False)
    with pytest.raises(TwilioNotConfiguredError):
        async with make_session() as s:
            await sync_twilio_numbers_to_pool(s, fake)
