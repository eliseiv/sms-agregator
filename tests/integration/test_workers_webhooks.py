"""Integration (coverage): delivery_retry_loop и проверка подписи Twilio."""

from __future__ import annotations

import asyncio

import pytest

from app.application.workers import delivery_retry_loop
from shared.config import get_settings
from tests.conftest import FakeTelegram

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_delivery_retry_loop_one_iteration_then_stop(monkeypatch):
    import app.application.workers as w

    monkeypatch.setattr(w, "get_telegram_client", lambda settings: FakeTelegram())
    settings = get_settings()
    stop = asyncio.Event()
    task = asyncio.create_task(delivery_retry_loop(settings, stop))
    await asyncio.sleep(0.2)  # дать выполниться одной итерации
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert task.done()


async def test_webhook_missing_token_500(client):
    settings = get_settings()
    orig_verify = settings.VERIFY_TWILIO_SIGNATURE
    orig_token = settings.TWILIO_AUTH_TOKEN
    try:
        settings.VERIFY_TWILIO_SIGNATURE = True
        settings.TWILIO_AUTH_TOKEN = ""
        r = await client.post(
            "/api/webhooks/twilio/sms",
            content="MessageSid=SM1&From=%2B1&To=%2B2&Body=x",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 500
    finally:
        settings.VERIFY_TWILIO_SIGNATURE = orig_verify
        settings.TWILIO_AUTH_TOKEN = orig_token


async def test_webhook_bad_signature_401(client):
    settings = get_settings()
    orig_verify = settings.VERIFY_TWILIO_SIGNATURE
    orig_token = settings.TWILIO_AUTH_TOKEN
    try:
        settings.VERIFY_TWILIO_SIGNATURE = True
        settings.TWILIO_AUTH_TOKEN = "some-token"
        r = await client.post(
            "/api/webhooks/twilio/sms",
            content="MessageSid=SM2&From=%2B1&To=%2B2&Body=x",
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": "invalid-signature",
            },
        )
        assert r.status_code == 401
    finally:
        settings.VERIFY_TWILIO_SIGNATURE = orig_verify
        settings.TWILIO_AUTH_TOKEN = orig_token
