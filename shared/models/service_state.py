"""ServiceState — key/value состояние сервиса. DDL: docs/04 таблица ``service_state``."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class ServiceState(Base):
    __tablename__ = "service_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
