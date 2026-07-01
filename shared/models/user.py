"""User-модель (super_admin / group_leader / group_member).

DDL: docs/04-data-model.md таблица ``users``.
"""

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
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base

if TYPE_CHECKING:
    from shared.models.team import Team


ROLE_SUPER_ADMIN = "super_admin"
ROLE_GROUP_LEADER = "group_leader"
ROLE_GROUP_MEMBER = "group_member"
ALL_ROLES: frozenset[str] = frozenset(
    {ROLE_SUPER_ADMIN, ROLE_GROUP_LEADER, ROLE_GROUP_MEMBER}
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'group_member'"),
    )
    team_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "teams.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
            name="fk_users_team_id",
        ),
        nullable=True,
    )
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_reset_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    lockout_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # Membership: user принадлежит максимум одной команде. Disambiguated по
    # foreign_keys, т.к. teams.leader_user_id тоже связывает эти таблицы.
    team: Mapped[Team | None] = relationship(
        "Team",
        foreign_keys=[team_id],
        lazy="raise",
        primaryjoin="User.team_id == Team.id",
    )

    __table_args__ = (
        CheckConstraint(
            "username = lower(username)",
            name="ck_users_username_lower",
        ),
        CheckConstraint(
            "role IN ('super_admin', 'group_leader', 'group_member')",
            name="ck_users_role",
        ),
        CheckConstraint(
            "display_name IS NULL OR char_length(display_name) BETWEEN 1 AND 100",
            name="ck_users_display_name_length",
        ),
        CheckConstraint(
            "(role = 'super_admin'  AND team_id IS NULL) OR "
            "(role = 'group_leader' AND team_id IS NOT NULL) OR "
            "(role = 'group_member' AND team_id IS NOT NULL)",
            name="ck_users_role_team_invariant",
        ),
        # Не более одного super_admin в БД (partial-UNIQUE, defense-in-depth).
        Index(
            "users_single_super_admin",
            "role",
            unique=True,
            postgresql_where=text("role = 'super_admin'"),
        ),
        Index(
            "ix_users_team_id_partial",
            "team_id",
            postgresql_where=text("team_id IS NOT NULL"),
        ),
    )

    @property
    def is_super_admin(self) -> bool:
        return self.role == ROLE_SUPER_ADMIN

    @property
    def is_group_leader(self) -> bool:
        return self.role == ROLE_GROUP_LEADER

    @property
    def is_group_member(self) -> bool:
        return self.role == ROLE_GROUP_MEMBER
