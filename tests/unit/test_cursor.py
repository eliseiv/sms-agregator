"""Unit: opaque keyset-курсор пагинации SMS (docs/05 §9, ADR-0014 §4).

Round-trip позиции, устойчивость к битому/пустому токену, отказ от наивного
(без tz) datetime. Без БД/сети — чистый кодек.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.application.cursor import decode_cursor, encode_cursor
from app.exceptions import InvalidCursorError

_MSK = timezone(timedelta(hours=3))


def test_roundtrip_preserves_position() -> None:
    ts = datetime(2026, 7, 3, 12, 34, 56, 789012, tzinfo=timezone.utc)
    token = encode_cursor(ts, 4242)
    got_ts, got_id = decode_cursor(token)
    assert got_ts == ts
    assert got_id == 4242


def test_roundtrip_preserves_microseconds_and_offset() -> None:
    ts = datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=_MSK)
    got_ts, got_id = decode_cursor(encode_cursor(ts, 1))
    # Микросекунды и смещение не теряются (сравнение по абсолютному моменту).
    assert got_ts == ts
    assert got_ts.utcoffset() == ts.utcoffset()


def test_token_is_opaque_base64url_without_padding() -> None:
    token = encode_cursor(datetime(2026, 7, 3, tzinfo=timezone.utc), 7)
    assert "=" not in token  # без padding
    assert "|" not in token  # разделитель полезной нагрузки не течёт наружу
    # base64url-алфавит: '+'/'/' не встречаются.
    assert "+" not in token and "/" not in token


def test_empty_cursor_rejected() -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor("")


@pytest.mark.parametrize(
    "bad",
    [
        "!!!not-base64!!!",
        "@@@@",
        "%%%",
        " ",
        "zzzz zzzz",
    ],
)
def test_undecodable_cursor_rejected(bad: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(bad)


def test_structurally_wrong_payload_rejected() -> None:
    import base64

    # Валидный base64url, но полезная нагрузка не «<iso>|<id>».
    raw = base64.urlsafe_b64encode(b"garbage-without-separator").rstrip(b"=").decode()
    with pytest.raises(InvalidCursorError):
        decode_cursor(raw)


def test_non_integer_id_rejected() -> None:
    import base64

    raw = base64.urlsafe_b64encode(b"2026-07-03T00:00:00+00:00|notanint")
    token = raw.rstrip(b"=").decode()
    with pytest.raises(InvalidCursorError):
        decode_cursor(token)


def test_naive_datetime_rejected() -> None:
    import base64

    # Наивный (без tz) datetime = подделанный/повреждённый токен (позиция всегда
    # кодируется из TIMESTAMPTZ).
    raw = base64.urlsafe_b64encode(b"2026-07-03T00:00:00|5").rstrip(b"=").decode()
    with pytest.raises(InvalidCursorError):
        decode_cursor(raw)
