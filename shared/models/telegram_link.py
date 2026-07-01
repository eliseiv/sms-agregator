"""TelegramLink ORM (ADR-0004). DDL: docs/04-data-model.md таблица ``telegram_links``.

Одна строка на привязанный Telegram-аккаунт. PK — ``telegram_user_id`` (= chat_id),
что даёт атомарный upsert ``ON CONFLICT (telegram_user_id) DO UPDATE``. ``user_id``
— 1:N (без UNIQUE): один внутренний user может иметь несколько привязок (мягкий
потолок ``TG_MAX_LINKS_PER_USER``). Активна пока ``dead_at IS NULL``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class TelegramLink(Base):
    __tablename__ = "telegram_links"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_telegram_links_user_id"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    dead_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("telegram_links_user_id_idx", "user_id"),)
