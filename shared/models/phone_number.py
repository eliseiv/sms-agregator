"""PhoneNumber ORM (было: twilio_numbers). DDL: docs/04 таблица ``phone_numbers``."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    phone_number: Mapped[str] = mapped_column(Text, nullable=False)
    team_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_phone_numbers_team_id"),
        nullable=False,
    )
    added_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_phone_numbers_added_by"),
        nullable=True,
    )
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("phone_number", name="uq_phone_numbers_phone_number"),
        Index("ix_phone_numbers_team_id", "team_id"),
    )
