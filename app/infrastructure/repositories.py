"""Реализации репозиториев на AsyncSession (docs/03-architecture §infrastructure).

Каждый репозиторий принимает ``AsyncSession`` в конструкторе. Носители данных —
ORM-модели из ``shared.models``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Recipient
from shared.models import (
    ROLE_SUPER_ADMIN,
    AdminAudit,
    Delivery,
    InboundSms,
    PhoneNumber,
    ServiceState,
    Team,
    TelegramLink,
    User,
)

_LAST_ERROR_MAX = 1000


# --- Teams ------------------------------------------------------------------


class TeamRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list_all(self) -> list[Team]:
        stmt = select(Team).order_by(Team.name)
        return list((await self._s.execute(stmt)).scalars().all())

    async def get(self, team_id: int) -> Team | None:
        return await self._s.get(Team, team_id)

    async def get_by_name(self, name: str) -> Team | None:
        stmt = select(Team).where(func.lower(Team.name) == name.lower())
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def create(self, *, name: str, leader_user_id: int | None = None) -> Team:
        team = Team(name=name, leader_user_id=leader_user_id)
        self._s.add(team)
        await self._s.flush()
        await self._s.refresh(team)
        return team

    async def rename(self, *, team_id: int, name: str) -> None:
        await self._s.execute(
            update(Team)
            .where(Team.id == team_id)
            .values(name=name, updated_at=datetime.now(UTC))
        )

    async def set_leader(self, *, team_id: int, leader_user_id: int | None) -> None:
        await self._s.execute(
            update(Team)
            .where(Team.id == team_id)
            .values(leader_user_id=leader_user_id, updated_at=datetime.now(UTC))
        )

    async def delete(self, team_id: int) -> None:
        await self._s.execute(delete(Team).where(Team.id == team_id))

    async def member_counts(self, team_ids: list[int]) -> dict[int, int]:
        if not team_ids:
            return {}
        stmt = (
            select(User.team_id, func.count())
            .where(User.team_id.in_(team_ids))
            .group_by(User.team_id)
        )
        return {int(tid): int(cnt) for tid, cnt in (await self._s.execute(stmt)).all()}

    async def numbers_counts(self, team_ids: list[int]) -> dict[int, int]:
        if not team_ids:
            return {}
        stmt = (
            select(PhoneNumber.team_id, func.count())
            .where(PhoneNumber.team_id.in_(team_ids))
            .group_by(PhoneNumber.team_id)
        )
        return {int(tid): int(cnt) for tid, cnt in (await self._s.execute(stmt)).all()}


# --- Phone numbers ----------------------------------------------------------


class PhoneNumberRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list_all(self) -> list[PhoneNumber]:
        stmt = select(PhoneNumber).order_by(PhoneNumber.created_at.desc())
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_by_team(self, team_id: int) -> list[PhoneNumber]:
        stmt = (
            select(PhoneNumber)
            .where(PhoneNumber.team_id == team_id)
            .order_by(PhoneNumber.created_at.desc())
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def find_by_phone(self, phone_number: str) -> PhoneNumber | None:
        stmt = select(PhoneNumber).where(PhoneNumber.phone_number == phone_number)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get(self, number_id: int) -> PhoneNumber | None:
        return await self._s.get(PhoneNumber, number_id)

    async def create(
        self,
        *,
        phone_number: str,
        team_id: int,
        added_by_user_id: int | None,
        label: str | None,
    ) -> PhoneNumber:
        number = PhoneNumber(
            phone_number=phone_number,
            team_id=team_id,
            added_by_user_id=added_by_user_id,
            label=label,
        )
        self._s.add(number)
        await self._s.flush()
        await self._s.refresh(number)
        return number

    async def delete(self, number_id: int) -> None:
        await self._s.execute(delete(PhoneNumber).where(PhoneNumber.id == number_id))


# --- Users ------------------------------------------------------------------


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_by_id(self, user_id: int) -> User | None:
        return await self._s.get(User, user_id)

    async def get_by_username(self, username: str) -> User | None:
        stmt = select(User).where(User.username == username.lower())
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_super_admin(self) -> User | None:
        stmt = select(User).where(User.role == ROLE_SUPER_ADMIN).limit(1)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> list[User]:
        stmt = select(User).order_by(User.team_id.asc().nullsfirst(), User.id)
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_by_team(self, team_id: int) -> list[User]:
        stmt = select(User).where(User.team_id == team_id).order_by(User.id)
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_user_ids_in_team(self, team_id: int) -> list[int]:
        stmt = select(User.id).where(User.team_id == team_id)
        return [int(row[0]) for row in (await self._s.execute(stmt)).all()]

    async def count_in_team(self, team_id: int) -> int:
        stmt = select(func.count()).select_from(User).where(User.team_id == team_id)
        return int((await self._s.execute(stmt)).scalar_one())

    async def recipients_for_team(self, team_id: int) -> list[Recipient]:
        """Пары (user_id, telegram_user_id) для участников команды с живой привязкой."""
        stmt = (
            select(User.id, TelegramLink.telegram_user_id)
            .join(TelegramLink, TelegramLink.user_id == User.id)
            .where(User.team_id == team_id, TelegramLink.dead_at.is_(None))
        )
        return [
            Recipient(user_id=int(uid), telegram_user_id=int(tg))
            for uid, tg in (await self._s.execute(stmt)).all()
        ]

    async def create(
        self,
        *,
        username: str,
        role: str,
        team_id: int | None,
        display_name: str | None = None,
        password_hash: str | None = None,
        password_reset_required: bool = True,
    ) -> User:
        user = User(
            username=username.lower(),
            role=role,
            team_id=team_id,
            display_name=display_name,
            password_hash=password_hash,
            password_reset_required=password_reset_required,
        )
        self._s.add(user)
        await self._s.flush()
        await self._s.refresh(user)
        return user

    async def set_password_hash(self, user_id: int, password_hash: str) -> None:
        await self._s.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                password_hash=password_hash,
                password_reset_required=False,
                failed_login_attempts=0,
                lockout_until=None,
                updated_at=datetime.now(UTC),
            )
        )

    async def reset_password(self, user_id: int) -> None:
        await self._s.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                password_hash=None,
                password_reset_required=True,
                failed_login_attempts=0,
                lockout_until=None,
                updated_at=datetime.now(UTC),
            )
        )

    async def update_fields(self, user_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = datetime.now(UTC)
        await self._s.execute(update(User).where(User.id == user_id).values(**fields))

    async def delete(self, user_id: int) -> None:
        await self._s.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    async def record_login_success(self, user_id: int) -> None:
        now = datetime.now(UTC)
        await self._s.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                failed_login_attempts=0,
                lockout_until=None,
                last_login_at=now,
                updated_at=now,
            )
        )

    async def record_login_failure(
        self, user_id: int, *, threshold: int, lockout_minutes: int
    ) -> tuple[int, datetime | None]:
        now = datetime.now(UTC)
        lockout_at = now + timedelta(minutes=lockout_minutes)
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(
                failed_login_attempts=User.failed_login_attempts + 1,
                lockout_until=case(
                    (User.failed_login_attempts + 1 >= threshold, lockout_at),
                    else_=User.lockout_until,
                ),
                updated_at=now,
            )
            .returning(User.failed_login_attempts, User.lockout_until)
        )
        row = (await self._s.execute(stmt)).one()
        return int(row[0]), row[1]


# --- Telegram links ---------------------------------------------------------


class TelegramLinkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_by_telegram_user_id(
        self, telegram_user_id: int
    ) -> TelegramLink | None:
        return await self._s.get(TelegramLink, telegram_user_id)

    async def get_active_by_telegram_user_id(
        self, telegram_user_id: int
    ) -> TelegramLink | None:
        stmt = select(TelegramLink).where(
            TelegramLink.telegram_user_id == telegram_user_id,
            TelegramLink.dead_at.is_(None),
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_by_user_id(self, user_id: int) -> list[TelegramLink]:
        stmt = (
            select(TelegramLink)
            .where(TelegramLink.user_id == user_id)
            .order_by(TelegramLink.created_at.desc())
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def count_active_by_user_id(self, user_id: int) -> int:
        stmt = select(func.count()).where(
            TelegramLink.user_id == user_id, TelegramLink.dead_at.is_(None)
        )
        return int((await self._s.execute(stmt)).scalar_one())

    async def upsert(
        self, *, telegram_user_id: int, user_id: int
    ) -> tuple[TelegramLink, bool]:
        existing = await self._s.get(TelegramLink, telegram_user_id)
        replaced = existing is not None
        stmt = (
            pg_insert(TelegramLink)
            .values(telegram_user_id=telegram_user_id, user_id=user_id)
            .on_conflict_do_update(
                index_elements=[TelegramLink.telegram_user_id],
                set_={
                    "user_id": user_id,
                    "created_at": datetime.now(UTC),
                    "dead_at": None,
                },
            )
            .returning(TelegramLink)
        )
        row = (await self._s.execute(stmt)).scalar_one()
        return row, replaced

    async def delete_all_by_user_id(self, user_id: int) -> list[int]:
        stmt = (
            delete(TelegramLink)
            .where(TelegramLink.user_id == user_id)
            .returning(TelegramLink.telegram_user_id)
        )
        return [int(row[0]) for row in (await self._s.execute(stmt)).all()]

    async def delete_one(self, *, user_id: int, telegram_user_id: int) -> bool:
        stmt = (
            delete(TelegramLink)
            .where(
                TelegramLink.user_id == user_id,
                TelegramLink.telegram_user_id == telegram_user_id,
            )
            .returning(TelegramLink.telegram_user_id)
        )
        return (await self._s.execute(stmt)).first() is not None

    async def mark_dead(self, telegram_user_id: int) -> None:
        await self._s.execute(
            update(TelegramLink)
            .where(
                TelegramLink.telegram_user_id == telegram_user_id,
                TelegramLink.dead_at.is_(None),
            )
            .values(dead_at=datetime.now(UTC))
        )

    async def users_with_active_link(self, user_ids: list[int]) -> set[int]:
        if not user_ids:
            return set()
        stmt = (
            select(TelegramLink.user_id)
            .where(TelegramLink.user_id.in_(user_ids), TelegramLink.dead_at.is_(None))
            .distinct()
        )
        return {int(row[0]) for row in (await self._s.execute(stmt)).all()}


# --- Inbound SMS ------------------------------------------------------------


class SmsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        twilio_message_sid: str | None,
        from_number: str,
        to_number: str,
        body: str,
        team_id: int | None,
        raw_payload: dict[str, Any],
        received_at: datetime | None = None,
    ) -> InboundSms:
        sms = InboundSms(
            twilio_message_sid=twilio_message_sid,
            from_number=from_number,
            to_number=to_number,
            body=body,
            team_id=team_id,
            raw_payload=raw_payload,
            received_at=received_at or datetime.now(UTC),
        )
        self._s.add(sms)
        await self._s.flush()
        await self._s.refresh(sms)
        return sms

    async def get(self, sms_id: int) -> InboundSms | None:
        return await self._s.get(InboundSms, sms_id)

    async def find_by_sid(self, sid: str) -> InboundSms | None:
        stmt = select(InboundSms).where(InboundSms.twilio_message_sid == sid).limit(1)
        return (await self._s.execute(stmt)).scalar_one_or_none()


# --- Deliveries -------------------------------------------------------------


class DeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def try_reserve(
        self, *, inbound_sms_id: int, user_id: int, telegram_user_id: int
    ) -> int | None:
        """Зарезервировать доставку в конкретный чат. None → уже была (идемпотентность)."""
        stmt = (
            pg_insert(Delivery)
            .values(
                inbound_sms_id=inbound_sms_id,
                user_id=user_id,
                telegram_user_id=telegram_user_id,
                status="pending",
            )
            .on_conflict_do_nothing(
                index_elements=[Delivery.inbound_sms_id, Delivery.telegram_user_id]
            )
            .returning(Delivery.id)
        )
        row = (await self._s.execute(stmt)).first()
        return int(row[0]) if row is not None else None

    async def get(self, delivery_id: int) -> Delivery | None:
        return await self._s.get(Delivery, delivery_id)

    async def mark_sent(self, delivery_id: int) -> None:
        now = datetime.now(UTC)
        await self._s.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(
                status="sent",
                sent_at=now,
                attempts=Delivery.attempts + 1,
                last_error=None,
                updated_at=now,
            )
        )

    async def mark_failed(self, delivery_id: int, error_message: str) -> None:
        await self._s.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(
                status="failed",
                attempts=Delivery.attempts + 1,
                last_error=error_message[:_LAST_ERROR_MAX],
                updated_at=datetime.now(UTC),
            )
        )

    async def mark_dead(self, delivery_id: int, error_message: str) -> None:
        await self._s.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(
                status="dead",
                attempts=Delivery.attempts + 1,
                last_error=error_message[:_LAST_ERROR_MAX],
                updated_at=datetime.now(UTC),
            )
        )

    async def pending(self, max_attempts: int, limit: int = 100) -> list[Delivery]:
        stmt = (
            select(Delivery)
            .where(
                Delivery.status.in_(("pending", "failed")),
                Delivery.attempts < max_attempts,
            )
            .order_by(Delivery.id)
            .limit(limit)
        )
        return list((await self._s.execute(stmt)).scalars().all())


# --- Service state ----------------------------------------------------------


class StateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, key: str) -> str | None:
        row = await self._s.get(ServiceState, key)
        return row.value if row is not None else None

    async def set(self, key: str, value: str) -> None:
        stmt = (
            pg_insert(ServiceState)
            .values(key=key, value=value)
            .on_conflict_do_update(
                index_elements=[ServiceState.key],
                set_={"value": value, "updated_at": datetime.now(UTC)},
            )
        )
        await self._s.execute(stmt)


# --- Admin audit ------------------------------------------------------------


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert(
        self,
        *,
        actor_user_id: int,
        action: str,
        target_user_id: int | None = None,
        target_username: str | None = None,
        details: dict[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> AdminAudit:
        if user_agent:
            user_agent = user_agent[:256]
        record = AdminAudit(
            actor_user_id=actor_user_id,
            action=action,
            target_user_id=target_user_id,
            target_username=target_username,
            details=details,
            ip=ip,
            user_agent=user_agent,
        )
        self._s.add(record)
        await self._s.flush()
        return record
