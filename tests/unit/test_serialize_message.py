"""Unit: serialize_message — ровно безопасное подмножество полей (docs/05 §9).

`{id, from_number, to_number, body, received_at, team_id}` и НИЧЕГО больше;
`raw_payload` не раскрывается; `received_at` — ISO 8601 с таймзоной.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.serializers import serialize_message
from shared.models import InboundSms

_MSK = timezone(timedelta(hours=3))


def _make_sms(**overrides: object) -> InboundSms:
    defaults: dict[str, object] = {
        "id": 101,
        "twilio_message_sid": "SM-secret-sid",
        "from_number": "+12025550001",
        "to_number": "+441234500001",
        "body": "hello",
        "team_id": 7,
        "raw_payload": {"AccountSid": "ACxxxx", "ApiSecret": "must-not-leak"},
        "received_at": datetime(2026, 7, 3, 10, 20, 30, tzinfo=_MSK),
    }
    defaults.update(overrides)
    return InboundSms(**defaults)


def test_exact_field_set() -> None:
    out = serialize_message(_make_sms())
    assert set(out.keys()) == {
        "id",
        "from_number",
        "to_number",
        "body",
        "received_at",
        "team_id",
    }


def test_raw_payload_and_sid_not_exposed() -> None:
    out = serialize_message(_make_sms())
    assert "raw_payload" not in out
    assert "twilio_message_sid" not in out
    # Значения секретов из raw_payload не просачиваются ни в одно поле.
    assert "must-not-leak" not in repr(out)


def test_values_passthrough() -> None:
    out = serialize_message(_make_sms())
    assert out["id"] == 101
    assert out["from_number"] == "+12025550001"
    assert out["to_number"] == "+441234500001"
    assert out["body"] == "hello"
    assert out["team_id"] == 7


def test_received_at_is_iso8601_with_tz() -> None:
    out = serialize_message(_make_sms())
    received = out["received_at"]
    assert isinstance(received, str)
    # ISO 8601 с таймзоной: смещение присутствует (+03:00 для MSK).
    parsed = datetime.fromisoformat(received)
    assert parsed.tzinfo is not None
    assert received.endswith("+03:00")


def test_team_id_snapshot_may_be_null() -> None:
    out = serialize_message(_make_sms(team_id=None))
    assert out["team_id"] is None
