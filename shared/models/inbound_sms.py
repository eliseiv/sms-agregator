"""InboundSms ORM (было: inbound_messages). DDL: docs/04 таблица ``inbound_sms``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class InboundSms(Base):
    __tablename__ = "inbound_sms"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    twilio_message_sid: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_number: Mapped[str] = mapped_column(Text, nullable=False)
    to_number: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    team_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("teams.id", ondelete="SET NULL", name="fk_inbound_sms_team_id"),
        nullable=True,
    )
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # partial-UNIQUE для дедупликации ретраев webhook (NULL-SID не конфликтуют).
        Index(
            "inbound_sms_sid_uq",
            "twilio_message_sid",
            unique=True,
            postgresql_where=text("twilio_message_sid IS NOT NULL"),
        ),
        Index("ix_inbound_sms_received_at", text("received_at DESC")),
        Index(
            "ix_inbound_sms_team_id_partial",
            "team_id",
            postgresql_where=text("team_id IS NOT NULL"),
        ),
    )
