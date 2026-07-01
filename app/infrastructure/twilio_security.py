from __future__ import annotations

from twilio.request_validator import RequestValidator  # type: ignore[import-not-found, import-untyped]


def validate_twilio_signature(
    *,
    auth_token: str,
    signature: str | None,
    url: str,
    form_data: dict[str, str],
) -> bool:
    if not signature:
        return False
    validator = RequestValidator(auth_token)
    return validator.validate(url, form_data, signature)
