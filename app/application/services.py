"""SMS-пайплайн (docs/03-architecture §Поток приёма и рассылки SMS).

``handle_incoming_sms`` — приём webhook: нормализация → поиск номера → дедуп по
SID → сохранение inbound_sms → рассылка получателям команды (try_reserve +
send). ``retry_pending_deliveries`` — фоновая переотправка.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.services import normalize_phone
from app.infrastructure.repositories import (
    DeliveryRepository,
    PhoneNumberRepository,
    SmsRepository,
    TelegramLinkRepository,
    UserRepository,
)
from app.infrastructure.telegram_api import (
    TelegramApiClient,
    TelegramApiError,
    TelegramForbiddenError,
)
from shared.config import Settings
from shared.logging import get_logger
from shared.models import InboundSms

log = get_logger(__name__)

TELEGRAM_MESSAGE_LIMIT = 3500


def format_sms_message(settings: Settings, sms: InboundSms) -> str:
    zone = ZoneInfo(settings.TIMEZONE)
    local_time = sms.received_at.astimezone(zone).strftime("%d.%m %H:%M")
    return (
        "📩 Новое SMS\n\n"
        f"📱 Номер: {sms.to_number}\n"
        f"👤 От: {sms.from_number}\n"
        f"💬 Текст: {sms.body}\n"
        f"🕒 Время: {local_time}"
    )


def _split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        if len(line) <= limit:
            current = line
            continue
        start = 0
        while start < len(line):
            parts.append(line[start : start + limit])
            start += limit
        current = ""
    if current:
        parts.append(current)
    return parts or [text[:limit]]


async def _send_text(telegram: TelegramApiClient, chat_id: int, text: str) -> None:
    for part in _split_message(text):
        await telegram.send_message(chat_id, part)


async def handle_incoming_sms(
    session: AsyncSession,
    telegram: TelegramApiClient,
    settings: Settings,
    *,
    twilio_message_sid: str | None,
    from_number: str,
    to_number: str,
    body: str,
    raw_payload: dict[str, str],
) -> InboundSms:
    """Обработать входящее SMS: сохранить и разослать получателям команды.

    Дедупликация по ``twilio_message_sid`` (partial-UNIQUE). Crash-recoverable
    fan-out (docs/03 §«Восстановимость веерной рассылки», ADR-0005 §4): и новый
    SMS, и дубликат-ветка (webhook-retry по тому же MessageSid) ведут в общий
    fan-out — ``try_reserve`` идемпотентен (ON CONFLICT), поэтому retry Twilio
    добирает получателей, которым доставка не была создана до крэша. Уже
    зарезервированные pending-строки добивает ``retry_pending_deliveries``.
    """
    normalized_to = normalize_phone(to_number)
    normalized_from = normalize_phone(from_number)

    numbers = PhoneNumberRepository(session)
    sms_repo = SmsRepository(session)

    # 1. Сохранение / резолв SMS (дедуп по SID). Дубликат НЕ делает ранний
    #    возврат — падаем в общий fan-out ниже (crash-recovery).
    sms: InboundSms | None = None
    try:
        async with session.begin():
            if twilio_message_sid:
                sms = await sms_repo.find_by_sid(twilio_message_sid)
                if sms is not None:
                    log.info("sms_duplicate_sid", sid=twilio_message_sid)
            if sms is None:
                number = await numbers.find_by_phone(normalized_to)
                team_id = number.team_id if number is not None else None
                sms = await sms_repo.create(
                    twilio_message_sid=twilio_message_sid,
                    from_number=normalized_from,
                    to_number=normalized_to,
                    body=body,
                    team_id=team_id,
                    raw_payload=dict(raw_payload),
                )
    except IntegrityError:
        # Конкурентный webhook с тем же MessageSid уронил insert на partial-UNIQUE.
        # Читаем уже сохранённый SMS и продолжаем в общий fan-out (идемпотентно).
        if not twilio_message_sid:
            raise
        async with session.begin():
            sms = await sms_repo.find_by_sid(twilio_message_sid)
        if sms is None:
            raise
        log.info("sms_duplicate_sid_race", sid=twilio_message_sid)

    assert sms is not None
    sms_id = sms.id
    sms_team_id = sms.team_id

    if sms_team_id is None:
        log.warning("sms_unknown_number", to_number=normalized_to)
        return sms

    # 2. Резолв получателей команды (снапшот на момент обработки).
    recipients = await UserRepository(session).recipients_for_team(sms_team_id)
    if not recipients:
        log.warning("sms_no_recipients", team_id=sms_team_id)
        return sms

    # Закрыть autobegun read-tx от recipients_for_team перед write-транзакциями.
    await session.commit()

    # 3. Fan-out: идемпотентное резервирование + отправка на каждый чат.
    for recipient in recipients:
        await _deliver(
            session,
            telegram,
            settings,
            sms=sms,
            sms_id=sms_id,
            user_id=recipient.user_id,
            telegram_user_id=recipient.telegram_user_id,
        )
    return sms


async def _deliver(
    session: AsyncSession,
    telegram: TelegramApiClient,
    settings: Settings,
    *,
    sms: InboundSms,
    sms_id: int,
    user_id: int,
    telegram_user_id: int,
) -> None:
    deliveries = DeliveryRepository(session)

    async with session.begin():
        delivery_id = await deliveries.try_reserve(
            inbound_sms_id=sms_id, user_id=user_id, telegram_user_id=telegram_user_id
        )
    if delivery_id is None:
        return  # уже доставлялось (идемпотентность)

    await deliver_sms_to_recipient(
        session,
        telegram,
        settings,
        sms=sms,
        delivery_id=delivery_id,
        user_id=user_id,
        telegram_user_id=telegram_user_id,
    )


async def deliver_sms_to_recipient(
    session: AsyncSession,
    telegram: TelegramApiClient,
    settings: Settings,
    *,
    sms: InboundSms,
    delivery_id: int,
    user_id: int,
    telegram_user_id: int,
) -> None:
    """Отправить SMS одному получателю и зафиксировать статус доставки."""
    deliveries = DeliveryRepository(session)
    links = TelegramLinkRepository(session)

    if not telegram.is_configured:
        async with session.begin():
            await deliveries.mark_failed(delivery_id, "Telegram bot token не настроен")
        return

    try:
        await _send_text(telegram, telegram_user_id, format_sms_message(settings, sms))
    except TelegramForbiddenError as exc:
        async with session.begin():
            await deliveries.mark_dead(delivery_id, str(exc))
            await links.mark_dead(telegram_user_id)
        log.warning("sms_delivery_dead", telegram_user_id=telegram_user_id)
        return
    except TelegramApiError as exc:
        async with session.begin():
            await deliveries.mark_failed(delivery_id, str(exc))
        log.warning("sms_delivery_failed", telegram_user_id=telegram_user_id)
        return

    async with session.begin():
        await deliveries.mark_sent(delivery_id)


async def retry_pending_deliveries(
    session: AsyncSession, telegram: TelegramApiClient, settings: Settings
) -> int:
    """Переотправить pending/failed доставки (chat_id из снимка delivery)."""
    deliveries = DeliveryRepository(session)
    sms_repo = SmsRepository(session)
    links = TelegramLinkRepository(session)

    pending = await deliveries.pending(settings.DELIVERY_MAX_ATTEMPTS)
    # Закрыть autobegun read-tx от pending() перед write-транзакциями.
    await session.commit()
    retried = 0
    for delivery in pending:
        # Сначала все read-запросы итерации, затем закрываем read-tx.
        sms = await sms_repo.get(delivery.inbound_sms_id)
        active_link = (
            await links.get_active_by_telegram_user_id(delivery.telegram_user_id)
            if sms is not None
            else None
        )
        await session.commit()  # закрыть autobegun read-tx перед session.begin()
        if sms is None:
            async with session.begin():
                await deliveries.mark_failed(delivery.id, "Исходное SMS не найдено")
            continue
        # Проверка живости привязки по снимку chat_id.
        if active_link is None:
            async with session.begin():
                await deliveries.mark_dead(delivery.id, "Привязка Telegram недоступна")
            continue
        await deliver_sms_to_recipient(
            session,
            telegram,
            settings,
            sms=sms,
            delivery_id=delivery.id,
            user_id=delivery.user_id,
            telegram_user_id=delivery.telegram_user_id,
        )
        retried += 1
    return retried
