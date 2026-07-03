"""Общие сериализаторы API-ресурсов.

Единый сериализатор номера — переиспользуется в ``/api/numbers`` (§6),
``/app`` (SSR-инжект) и ``/api/admin/numbers`` (§4a), чтобы форма ответа была
одинаковой (docs/05-api-contracts).
"""

from __future__ import annotations

from typing import Any

from shared.models import InboundSms, PhoneNumber


def serialize_number(number: PhoneNumber, team_name: str | None) -> dict[str, Any]:
    """Сериализовать номер. ``team_name`` = None для unassigned (team_id IS NULL)."""
    return {
        "id": number.id,
        "phone_number": number.phone_number,
        "team_id": number.team_id,
        "team_name": team_name,
        "label": number.label,
        "is_active": number.is_active,
        "added_by_user_id": number.added_by_user_id,
        "created_at": number.created_at.isoformat(),
    }


def serialize_message(sms: InboundSms) -> dict[str, Any]:
    """Сериализовать входящее SMS для просмотра (docs/05 §9, ADR-0014).

    Безопасное подмножество полей ``inbound_sms``; ``raw_payload`` **не
    отдаётся** (может содержать служебные/чувствительные данные Twilio).
    ``team_id`` — исторический снимок команды на момент приёма (для видимости
    не используется). ``received_at`` — ISO 8601 с таймзоной.
    """
    return {
        "id": sms.id,
        "from_number": sms.from_number,
        "to_number": sms.to_number,
        "body": sms.body,
        "received_at": sms.received_at.isoformat(),
        "team_id": sms.team_id,
    }
