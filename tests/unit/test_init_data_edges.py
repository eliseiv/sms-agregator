"""Unit (coverage): краевые ветки verify_init_data."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

from app.telegram.init_data import ValidatedInitData, verify_init_data
from tests.conftest import TEST_BOT_TOKEN

NOW = 1_700_000_000


def _sign(fields: dict[str, str]) -> str:
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode(), hashlib.sha256).digest()
    out = dict(fields)
    out["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(out)


def test_strict_parsing_malformed():
    assert (
        verify_init_data("justtext", bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW)
        == "malformed"
    )


def test_missing_auth_date():
    raw = _sign({"user": '{"id":1}', "query_id": "AAA"})
    assert (
        verify_init_data(raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW)
        == "missing_auth_date"
    )


def test_auth_date_not_int():
    raw = _sign({"auth_date": "not-a-number", "user": '{"id":1}'})
    assert (
        verify_init_data(raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW)
        == "missing_auth_date"
    )


def test_invalid_user_json():
    raw = _sign({"auth_date": str(NOW), "user": "{bad json"})
    assert (
        verify_init_data(raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW)
        == "invalid_user_payload"
    )


def test_user_id_not_int():
    raw = _sign({"auth_date": str(NOW), "user": '{"id":"abc"}'})
    assert (
        verify_init_data(raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW)
        == "invalid_user_payload"
    )


def test_nonstring_names_coerced_to_none():
    raw = _sign(
        {"auth_date": str(NOW), "user": '{"id":9,"first_name":123,"username":456}'}
    )
    result = verify_init_data(
        raw, bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=NOW
    )
    assert isinstance(result, ValidatedInitData)
    assert result.first_name is None
    assert result.username is None
