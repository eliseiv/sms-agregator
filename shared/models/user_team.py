"""UserTeam — аддитивное M:N членство «пользователь ↔ команда» (ADR-0012).

Источник истины для адресации SMS (``recipients_for_team``), видимости/добавления
номеров в ``/app`` и подсчёта участников команды. ``users.team_id`` остаётся
**домашней**/первичной командой; каждое домашнее членство зеркалируется строкой
здесь (backfill в ревизии ``20260702_003`` + синхронизация в ``AdminService``).

DDL: docs/04-data-model.md таблица ``user_teams`` + ADR-0012.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class UserTeam(Base):
    __tablename__ = "user_teams"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_user_teams_user_id"),
        nullable=False,
    )
    team_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_user_teams_team_id"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # Членство не дублируется; обслуживает и lookup (user_id, team_id) для
        # ``exists``, и гарантирует идемпотентность ``POST .../teams`` (ADR-0012).
        UniqueConstraint("user_id", "team_id", name="uq_user_teams_user_team"),
        # Обратный lookup «участники команды» (recipients_for_team / member-списки).
        Index("user_teams_team_id_idx", "team_id"),
    )
