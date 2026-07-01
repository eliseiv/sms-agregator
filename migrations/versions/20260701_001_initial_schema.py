"""Initial schema — все 8 таблиц + триггеры + CHECK + индексы.

Зеркалит docs/04-data-model.md. Циклический FK teams.leader_user_id ↔
users.team_id разрешается: teams создаётся без leader-FK, users создаётся с
team_id FK (DEFERRABLE), затем leader-FK добавляется ALTER-ом.

Revision ID: 20260701_001
Revises:
Create Date: 2026-07-01 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260701_001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UPDATED_AT_FN = """
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_LEADER_FN = """
CREATE OR REPLACE FUNCTION users_team_leader_consistency_check()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.role = 'group_leader' THEN
    IF NOT EXISTS (
        SELECT 1 FROM teams t
        WHERE t.id = NEW.team_id AND t.leader_user_id = NEW.id
    ) THEN
      RAISE EXCEPTION
        'user % is group_leader but team % has a different leader',
        NEW.id, NEW.team_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _updated_at_trigger(table: str) -> str:
    return (
        f"CREATE TRIGGER trg_{table}_updated_at "
        f"BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )


def upgrade() -> None:
    op.execute(_UPDATED_AT_FN)

    # ---- teams (без leader-FK; добавим ALTER-ом после users) ------------
    op.create_table(
        "teams",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("leader_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("name", name="uq_teams_name"),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 100", name="ck_teams_name_length"
        ),
    )
    op.create_index(
        "uq_teams_leader_user_id",
        "teams",
        ["leader_user_id"],
        unique=True,
        postgresql_where=sa.text("leader_user_id IS NOT NULL"),
    )
    op.execute(_updated_at_trigger("teams"))

    # ---- users (с team_id FK DEFERRABLE; teams уже существует) ----------
    op.create_table(
        "users",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False
        ),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column(
            "role", sa.Text(), nullable=False, server_default=sa.text("'group_member'")
        ),
        sa.Column("team_id", sa.BigInteger(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column(
            "password_reset_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("lockout_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "failed_login_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_users_team_id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint("username", name="users_username_key"),
        sa.CheckConstraint(
            "username = lower(username)", name="ck_users_username_lower"
        ),
        sa.CheckConstraint(
            "role IN ('super_admin', 'group_leader', 'group_member')",
            name="ck_users_role",
        ),
        sa.CheckConstraint(
            "display_name IS NULL OR char_length(display_name) BETWEEN 1 AND 100",
            name="ck_users_display_name_length",
        ),
        sa.CheckConstraint(
            "(role = 'super_admin'  AND team_id IS NULL) OR "
            "(role = 'group_leader' AND team_id IS NOT NULL) OR "
            "(role = 'group_member' AND team_id IS NOT NULL)",
            name="ck_users_role_team_invariant",
        ),
    )
    op.create_index(
        "users_single_super_admin",
        "users",
        ["role"],
        unique=True,
        postgresql_where=sa.text("role = 'super_admin'"),
    )
    op.create_index(
        "ix_users_team_id_partial",
        "users",
        ["team_id"],
        postgresql_where=sa.text("team_id IS NOT NULL"),
    )
    op.execute(_updated_at_trigger("users"))

    # Теперь цикл замкнут — добавляем leader-FK на teams.
    op.create_foreign_key(
        "fk_teams_leader_user_id",
        "teams",
        "users",
        ["leader_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Constraint-триггер лидерства (defense-in-depth).
    op.execute(_LEADER_FN)
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_users_team_leader_consistency "
        "AFTER INSERT OR UPDATE OF role, team_id ON users "
        "DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION users_team_leader_consistency_check();"
    )

    # ---- telegram_links -------------------------------------------------
    op.create_table(
        "telegram_links",
        sa.Column(
            "telegram_user_id", sa.BigInteger(), primary_key=True, nullable=False
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("dead_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_telegram_links_user_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("telegram_links_user_id_idx", "telegram_links", ["user_id"])

    # ---- phone_numbers --------------------------------------------------
    op.create_table(
        "phone_numbers",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False
        ),
        sa.Column("phone_number", sa.Text(), nullable=False),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column("added_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_phone_numbers_team_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["added_by_user_id"],
            ["users.id"],
            name="fk_phone_numbers_added_by",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("phone_number", name="uq_phone_numbers_phone_number"),
    )
    op.create_index("ix_phone_numbers_team_id", "phone_numbers", ["team_id"])
    op.execute(_updated_at_trigger("phone_numbers"))

    # ---- inbound_sms ----------------------------------------------------
    op.create_table(
        "inbound_sms",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False
        ),
        sa.Column("twilio_message_sid", sa.Text(), nullable=True),
        sa.Column("from_number", sa.Text(), nullable=False),
        sa.Column("to_number", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("team_id", sa.BigInteger(), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_inbound_sms_team_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "inbound_sms_sid_uq",
        "inbound_sms",
        ["twilio_message_sid"],
        unique=True,
        postgresql_where=sa.text("twilio_message_sid IS NOT NULL"),
    )
    op.execute(
        "CREATE INDEX ix_inbound_sms_received_at ON inbound_sms (received_at DESC)"
    )
    op.create_index(
        "ix_inbound_sms_team_id_partial",
        "inbound_sms",
        ["team_id"],
        postgresql_where=sa.text("team_id IS NOT NULL"),
    )

    # ---- deliveries -----------------------------------------------------
    op.create_table(
        "deliveries",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False
        ),
        sa.Column("inbound_sms_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["inbound_sms_id"],
            ["inbound_sms.id"],
            name="fk_deliveries_inbound_sms_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_deliveries_user_id", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'dead')",
            name="ck_deliveries_status",
        ),
        sa.UniqueConstraint(
            "inbound_sms_id", "telegram_user_id", name="deliveries_sms_chat_uq"
        ),
    )
    op.create_index(
        "ix_deliveries_retry_partial",
        "deliveries",
        ["status", "attempts"],
        postgresql_where=sa.text("status IN ('pending', 'failed')"),
    )
    op.execute(_updated_at_trigger("deliveries"))

    # ---- admin_audit (без FK) -------------------------------------------
    op.create_table(
        "admin_audit",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False
        ),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), nullable=True),
        sa.Column("target_username", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "CREATE INDEX ix_admin_audit_created_at_desc ON admin_audit (created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_admin_audit_actor_created_desc "
        "ON admin_audit (actor_user_id, created_at DESC)"
    )
    op.create_index(
        "ix_admin_audit_target_user_partial",
        "admin_audit",
        ["target_user_id"],
        postgresql_where=sa.text("target_user_id IS NOT NULL"),
    )

    # ---- service_state --------------------------------------------------
    op.create_table(
        "service_state",
        sa.Column("key", sa.Text(), primary_key=True, nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("service_state")
    op.drop_table("admin_audit")
    op.execute("DROP TRIGGER IF EXISTS trg_deliveries_updated_at ON deliveries")
    op.drop_table("deliveries")
    op.drop_table("inbound_sms")
    op.execute("DROP TRIGGER IF EXISTS trg_phone_numbers_updated_at ON phone_numbers")
    op.drop_table("phone_numbers")
    op.drop_table("telegram_links")
    op.execute("DROP TRIGGER IF EXISTS trg_users_team_leader_consistency ON users")
    op.execute("DROP FUNCTION IF EXISTS users_team_leader_consistency_check();")
    # Снять leader-FK до удаления users (иначе RESTRICT-цикл).
    op.drop_constraint("fk_teams_leader_user_id", "teams", type_="foreignkey")
    op.execute("DROP TRIGGER IF EXISTS trg_users_updated_at ON users")
    op.drop_table("users")
    op.execute("DROP TRIGGER IF EXISTS trg_teams_updated_at ON teams")
    op.drop_table("teams")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
