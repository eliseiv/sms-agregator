"""Доменные сущности.

Основные носители данных — ORM-модели из ``shared.models`` (Team, User,
PhoneNumber, InboundSms, Delivery, TelegramLink). Здесь определяются только
лёгкие value-объекты, не имеющие таблицы.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Recipient:
    """Получатель SMS: внутренний ``user_id`` + ``telegram_user_id`` (= chat_id).

    Формируется ``UserRepository.recipients_for_team`` (join users ↔
    telegram_links с ``dead_at IS NULL``).
    """

    user_id: int
    telegram_user_id: int
