"""Integration: POST /api/telegram/webhook (docs/06 §6, ADR-0010)."""

from __future__ import annotations

import pytest

from app.infrastructure.telegram_api import TelegramApiError
from tests.conftest import FakeTelegram

pytestmark = pytest.mark.asyncio(loop_scope="session")

SECRET = "test-webhook-secret-xyz"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


async def test_webhook_empty_secret_403(client):
    r = await client.post("/api/telegram/webhook", json={"message": {"text": "/start"}})
    assert r.status_code == 403
    assert r.json()["error"] == "invalid_webhook_secret"


async def test_webhook_wrong_secret_403(client):
    r = await client.post(
        "/api/telegram/webhook",
        json={"message": {"text": "/start"}},
        headers={SECRET_HEADER: "wrong-secret"},
    )
    assert r.status_code == 403


async def test_webhook_start_sends_webapp_button(client, monkeypatch):
    import app.api.routers.telegram_webhook as wh

    fake = FakeTelegram()
    monkeypatch.setattr(wh, "get_telegram_client", lambda settings: fake)
    r = await client.post(
        "/api/telegram/webhook",
        json={"message": {"text": "/start", "chat": {"id": 424242}}},
        headers={SECRET_HEADER: SECRET},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert fake.calls and fake.calls[0][0] == 424242
    markup = fake.markups[0]
    assert markup is not None
    btn = markup["inline_keyboard"][0][0]
    assert "web_app" in btn
    assert btn["web_app"]["url"] == "https://example.test/app"


async def test_webhook_other_update_noop_200(client):
    r = await client.post(
        "/api/telegram/webhook",
        json={"message": {"text": "hello there"}},
        headers={SECRET_HEADER: SECRET},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


async def test_webhook_send_error_still_200(client, monkeypatch):
    import app.api.routers.telegram_webhook as wh

    def boom(chat_id, text_):
        raise TelegramApiError("boom")

    monkeypatch.setattr(
        wh, "get_telegram_client", lambda settings: FakeTelegram(behavior=boom)
    )
    r = await client.post(
        "/api/telegram/webhook",
        json={"message": {"text": "/start", "chat": {"id": 5}}},
        headers={SECRET_HEADER: SECRET},
    )
    assert r.status_code == 200


async def test_webhook_is_csrf_exempt(client):
    # Без сессии/CSRF, но с секретом — не 403 csrf_failed (secret-token защита).
    r = await client.post(
        "/api/telegram/webhook",
        json={"message": {"text": "hi"}},
        headers={SECRET_HEADER: SECRET},
    )
    assert r.status_code == 200


def test_extract_start_chat_id_branches():
    from app.api.routers.telegram_webhook import _extract_start_chat_id, _webapp_markup

    assert (
        _extract_start_chat_id({"message": {"text": "/start", "chat": {"id": 7}}}) == 7
    )
    assert _extract_start_chat_id({"message": {"text": "hello"}}) is None
    assert _extract_start_chat_id({"message": "not-a-dict"}) is None
    assert (
        _extract_start_chat_id({"message": {"text": "/start", "chat": "bad"}}) is None
    )
    assert _extract_start_chat_id({"message": {"text": 123, "chat": {"id": 1}}}) is None
    markup = _webapp_markup("https://x.test/app")
    assert markup["inline_keyboard"][0][0]["web_app"]["url"] == "https://x.test/app"


async def test_webhook_rate_limit_per_ip(client):
    statuses = []
    for _ in range(65):
        r = await client.post(
            "/api/telegram/webhook",
            json={"message": {"text": "x"}},
            headers={SECRET_HEADER: "wrong", "X-Forwarded-For": "198.51.100.9"},
        )
        statuses.append(r.status_code)
    assert 429 in statuses, f"ожидался 429 при флуде, got {set(statuses)}"
