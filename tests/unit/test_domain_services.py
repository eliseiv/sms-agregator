"""Unit: normalize_phone, format_sms_message, _split_message (docs/06 §Unit)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.application.services import (
    TELEGRAM_MESSAGE_LIMIT,
    _split_message,
    format_sms_message,
)
from app.domain.services import normalize_phone
from shared.config import get_settings
from shared.models import InboundSms


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+1 (202) 555-0125", "+12025550125"),
        ("12025550125", "+12025550125"),
        ("0012025550125", "+12025550125"),
        ("+1-202-555-0125", "+12025550125"),
        ("", ""),
        ("   ", ""),
        ("tel:+441234567890", "+441234567890"),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def _sms(body: str) -> InboundSms:
    return InboundSms(
        twilio_message_sid="SM1",
        from_number="+12025550125",
        to_number="+441234567890",
        body=body,
        team_id=1,
        raw_payload={},
        received_at=datetime(2026, 7, 1, 9, 30, tzinfo=UTC),
    )


def test_format_sms_message_contains_fields():
    msg = format_sms_message(get_settings(), _sms("Hello world"))
    assert "+441234567890" in msg
    assert "+12025550125" in msg
    assert "Hello world" in msg
    # Europe/Moscow = UTC+3 → 09:30 UTC → 12:30.
    assert "12:30" in msg


def test_split_message_short_single_part():
    assert _split_message("hi") == ["hi"]


def test_split_message_splits_on_newlines():
    text = "\n".join(["line"] * 2000)  # > 3500 chars
    parts = _split_message(text)
    assert len(parts) > 1
    assert all(len(p) <= TELEGRAM_MESSAGE_LIMIT for p in parts)
    # Восстановление содержимого (без потери строк).
    assert sum(p.count("line") for p in parts) == 2000


def test_split_message_hard_split_long_line():
    text = "x" * (TELEGRAM_MESSAGE_LIMIT * 2 + 10)
    parts = _split_message(text)
    assert all(len(p) <= TELEGRAM_MESSAGE_LIMIT for p in parts)
    assert "".join(parts) == text
