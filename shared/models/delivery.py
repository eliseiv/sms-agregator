"""Delivery ORM (было: telegram_deliveries). DDL: docs/04 таблица ``deliveries``."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    inbound_sms_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "inbound_sms.id", ondelete="CASCADE", name="fk_deliveries_inbound_sms_id"
        ),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_deliveries_user_id"),
        nullable=False,
    )
    # Снимок chat_id на момент доставки. Без FK — реестр переживает
    # удаление/перепривязку линка.
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'dead')",
            name="ck_deliveries_status",
        ),
        UniqueConstraint(
            "inbound_sms_id",
            "telegram_user_id",
            name="deliveries_sms_chat_uq",
        ),
        Index(
            "ix_deliveries_retry_partial",
            "status",
            "attempts",
            postgresql_where=text("status IN ('pending', 'failed')"),
        ),
    )
