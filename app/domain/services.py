from __future__ import annotations

import re


def normalize_phone(value: str) -> str:
    digits = re.sub(r"[^\d+]", "", value or "")
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = f"+{digits[2:]}"
    if not digits.startswith("+"):
        digits = f"+{digits}"
    return digits
