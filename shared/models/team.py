"""Team-модель (было: projects). DDL: docs/04-data-model.md таблица ``teams``."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base

if TYPE_CHECKING:
    from shared.models.user import User


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL допустим только для orphan-команды без участников (до первого добавления).
    # UNIQUE → один user лидирует максимум одной командой. RESTRICT → нельзя
    # удалить пользователя, пока он лидер.
    leader_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_teams_leader_user_id"),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # `foreign_keys` обязателен: users.team_id тоже ссылается на teams.id.
    leader: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[leader_user_id],
        lazy="raise",
    )

    __table_args__ = (
        UniqueConstraint("name", name="uq_teams_name"),
        CheckConstraint(
            "char_length(name) BETWEEN 1 AND 100",
            name="ck_teams_name_length",
        ),
        Index(
            "uq_teams_leader_user_id",
            "leader_user_id",
            unique=True,
            postgresql_where=text("leader_user_id IS NOT NULL"),
        ),
    )
