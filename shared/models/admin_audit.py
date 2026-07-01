"""AdminAudit — append-only журнал действий. DDL: docs/04 таблица ``admin_audit``.

``actor_user_id`` / ``target_user_id`` — BIGINT без FK: запись переживает
удаление пользователя.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class AdminAudit(Base):
    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_admin_audit_created_at_desc", text("created_at DESC")),
        Index(
            "ix_admin_audit_actor_created_desc",
            "actor_user_id",
            text("created_at DESC"),
        ),
        Index(
            "ix_admin_audit_target_user_partial",
            "target_user_id",
            postgresql_where=text("target_user_id IS NOT NULL"),
        ),
    )
