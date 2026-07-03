"""Add ``user_teams`` M:N membership table (ADR-0012 multi-team).

Аддитивная join-таблица ``user_teams`` — источник истины для адресации SMS
(``recipients_for_team``), видимости/добавления номеров в ``/app`` и подсчёта
участников команды. ``users.team_id`` остаётся **домашней**/первичной командой и
**НЕ** удаляется; ``phone_numbers.team_id`` не трогается.

Backfill: для каждого ``users.team_id IS NOT NULL`` вставляем соответствующую
строку ``user_teams(user_id, team_id)`` — так домашнее членство всегда
зеркалируется (инвариант ADR-0012). ``super_admin`` имеет ``team_id IS NULL`` и не
попадает в backfill (инвариант «super_admin без членств»). ``ON CONFLICT DO
NOTHING`` делает backfill идемпотентным при повторном прогоне.

``downgrade``: ``DROP TABLE user_teams`` (данные членств теряются — обратимо только
структурно; домашние команды сохранены в ``users.team_id``).

Revision ID: 20260702_003
Revises: 20260702_002
Create Date: 2026-07-03 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260702_003"
down_revision: str | None = "20260702_002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_teams",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_teams_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_user_teams_team_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("user_id", "team_id", name="uq_user_teams_user_team"),
    )
    op.create_index("user_teams_team_id_idx", "user_teams", ["team_id"])

    # Backfill домашних членств из users.team_id (идемпотентно).
    op.execute(
        """
        INSERT INTO user_teams (user_id, team_id)
        SELECT id, team_id
        FROM   users
        WHERE  team_id IS NOT NULL
        ON CONFLICT (user_id, team_id) DO NOTHING
        """
    )


def downgrade() -> None:
    # ``users.team_id`` не удалялся, поэтому DROP TABLE полностью откатывает ревизию.
    op.drop_index("user_teams_team_id_idx", table_name="user_teams")
    op.drop_table("user_teams")
