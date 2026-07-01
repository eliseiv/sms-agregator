"""Unit: verify_init_data — HMAC/TTL/структура (docs/06 §Unit, docs/08 §5)."""

from __future__ import annotations

import time


from app.telegram.init_data import ValidatedInitData, verify_init_data
from tests.conftest import TEST_BOT_TOKEN, build_init_data


NOW = 1_700_000_000


def test_valid_hmac_returns_validated():
    raw = build_init_data(telegram_user_id=42, auth_date=NOW)
    result = verify_init_data(
        raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW
    )
    assert isinstance(result, ValidatedInitData)
    assert result.telegram_user_id == 42
    assert result.username == "tester"
    assert result.auth_date == NOW


def test_hash_mismatch():
    raw = build_init_data(telegram_user_id=42, auth_date=NOW, valid_hash=False)
    assert (
        verify_init_data(raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW)
        == "hash_mismatch"
    )


def test_wrong_bot_token_is_mismatch():
    raw = build_init_data(telegram_user_id=42, auth_date=NOW)
    assert (
        verify_init_data(raw, bot_token="999:OTHER", max_age_seconds=300, now=NOW)
        == "hash_mismatch"
    )


def test_expired_auth_date():
    raw = build_init_data(telegram_user_id=42, auth_date=NOW - 400)
    assert (
        verify_init_data(raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW)
        == "expired"
    )


def test_expired_boundary_not_expired():
    # current - auth_date == max_age → НЕ истёк (строгое >).
    raw = build_init_data(telegram_user_id=42, auth_date=NOW - 300)
    result = verify_init_data(
        raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW
    )
    assert isinstance(result, ValidatedInitData)


def test_missing_hash():
    raw = "auth_date=%d&user=%%7B%%22id%%22%%3A1%%7D" % NOW
    assert (
        verify_init_data(raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW)
        == "missing_hash"
    )


def test_missing_user():
    # Соберём валидный hash без user.
    import hashlib
    import hmac
    from urllib.parse import urlencode

    fields = {"auth_date": str(NOW), "query_id": "AAA"}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    raw = urlencode(fields)
    assert (
        verify_init_data(raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW)
        == "missing_user"
    )


def test_malformed_empty():
    assert (
        verify_init_data("", bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW)
        == "malformed"
    )


def test_malformed_no_bot_token():
    raw = build_init_data(telegram_user_id=1, auth_date=NOW)
    assert (
        verify_init_data(raw, bot_token="", max_age_seconds=300, now=NOW) == "malformed"
    )


def test_duplicate_keys_malformed():
    raw = build_init_data(telegram_user_id=1, auth_date=NOW) + "&auth_date=123"
    assert (
        verify_init_data(raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW)
        == "malformed"
    )


def test_invalid_user_payload_not_dict():
    import hashlib
    import hmac
    from urllib.parse import urlencode

    fields = {"auth_date": str(NOW), "user": "12345"}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    raw = urlencode(fields)
    assert (
        verify_init_data(raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW)
        == "invalid_user_payload"
    )


def test_default_now_uses_time(monkeypatch):
    raw = build_init_data(telegram_user_id=7, auth_date=int(time.time()))
    result = verify_init_data(raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300)
    assert isinstance(result, ValidatedInitData)
    assert result.telegram_user_id == 7
