"""Telegram WebApp ``initData`` HMAC-валидатор (docs/08-security §5).

Чистая функция: без I/O, БД, сайд-эффектов. Копия эталона mail-agregator.
Никогда не логирует raw initData (содержит токен и PII). Ошибки — как литералы.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl

InitDataError = Literal[
    "malformed",
    "missing_hash",
    "missing_user",
    "invalid_user_payload",
    "missing_auth_date",
    "hash_mismatch",
    "expired",
]


@dataclass(frozen=True, slots=True)
class ValidatedInitData:
    telegram_user_id: int
    first_name: str | None
    username: str | None
    auth_date: int  # unix seconds


def _build_data_check_string(pairs: list[tuple[str, str]]) -> str:
    filtered = [(k, v) for k, v in pairs if k != "hash"]
    filtered.sort(key=lambda kv: kv[0])
    return "\n".join(f"{k}={v}" for k, v in filtered)


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def verify_init_data(  # noqa: PLR0911 — каждый return = один named failure mode
    raw: str, *, bot_token: str, max_age_seconds: int, now: int | None = None
) -> ValidatedInitData | InitDataError:
    """Валидировать ``raw`` initData c ``bot_token``. Возвращает данные или литерал."""
    if not raw or not bot_token:
        return "malformed"

    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return "malformed"
    if not pairs:
        return "malformed"

    keys_seen: set[str] = set()
    for k, _ in pairs:
        if k in keys_seen:
            return "malformed"
        keys_seen.add(k)

    submitted_hash: str | None = None
    user_field: str | None = None
    auth_date_field: str | None = None
    for k, v in pairs:
        if k == "hash":
            submitted_hash = v
        elif k == "user":
            user_field = v
        elif k == "auth_date":
            auth_date_field = v

    if not submitted_hash:
        return "missing_hash"
    if user_field is None:
        return "missing_user"
    if auth_date_field is None:
        return "missing_auth_date"

    try:
        auth_date = int(auth_date_field)
    except ValueError:
        return "missing_auth_date"

    data_check_string = _build_data_check_string(pairs)
    computed = hmac.new(
        _secret_key(bot_token),
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(computed, submitted_hash):
        return "hash_mismatch"

    current = int(now if now is not None else time.time())
    if current - auth_date > max_age_seconds:
        return "expired"

    try:
        user_payload = json.loads(user_field)
    except (json.JSONDecodeError, TypeError):
        return "invalid_user_payload"
    if not isinstance(user_payload, dict):
        return "invalid_user_payload"

    raw_id = user_payload.get("id")
    if not isinstance(raw_id, int):
        return "invalid_user_payload"

    first_name = user_payload.get("first_name")
    if first_name is not None and not isinstance(first_name, str):
        first_name = None
    username = user_payload.get("username")
    if username is not None and not isinstance(username, str):
        username = None

    return ValidatedInitData(
        telegram_user_id=int(raw_id),
        first_name=first_name,
        username=username,
        auth_date=auth_date,
    )
