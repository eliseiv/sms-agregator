"""Opaque keyset-курсор для пагинации просмотра SMS (docs/05 §9, ADR-0014).

Кодирует **только позицию** — пару ``(received_at, id)`` последней отданной
строки — в непрозрачный для клиента ``base64url``-токен (без padding). Фильтры
(``to_number``/``team_id``/``limit``) курсор не несёт; клиент пересылает их
отдельно (ADR-0014 §4). Битый/недекодируемый токен → :class:`InvalidCursorError`
(маппится в ``400 invalid_cursor``).

Формат полезной нагрузки: ``"<received_at.isoformat()>|<id>"``. ``received_at`` —
timezone-aware datetime; ``isoformat``/``fromisoformat`` round-trip'ят значение
без потери точности (микросекунды + смещение). Разделитель ``|`` безопасен: в
ISO-8601 его нет, а ``id`` — целое.
"""

from __future__ import annotations

import base64
from datetime import datetime

from app.exceptions import InvalidCursorError

_SEP = "|"


def encode_cursor(received_at: datetime, row_id: int) -> str:
    """Закодировать позицию ``(received_at, id)`` в opaque base64url-токен."""
    raw = f"{received_at.isoformat()}{_SEP}{row_id}".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    """Декодировать opaque-курсор в ``(received_at, id)``.

    :raises InvalidCursorError: токен пуст, недекодируем или структурно неверен.
    """
    if not cursor:
        raise InvalidCursorError(detail="Пустой курсор")
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
        iso, id_str = raw.rsplit(_SEP, 1)
        received_at = datetime.fromisoformat(iso)
        row_id = int(id_str)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursorError(detail="Битый курсор пагинации") from exc
    if received_at.tzinfo is None:
        # Позиция всегда кодируется из TIMESTAMPTZ (tz-aware); наивный datetime =
        # подделанный/повреждённый токен.
        raise InvalidCursorError(detail="Курсор без таймзоны")
    return received_at, row_id
