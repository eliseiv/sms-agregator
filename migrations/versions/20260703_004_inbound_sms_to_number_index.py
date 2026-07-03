"""Add ``ix_inbound_sms_to_number_received`` for SMS viewing (ADR-0014).

Композитный индекс ``inbound_sms (to_number, received_at DESC, id DESC)`` —
эффективная фильтрация по номеру/набору номеров (``to_number IN (...)``) +
keyset-пагинация просмотра SMS (``ORDER BY received_at DESC, id DESC``, docs/05
§9). Глобальный путь super_admin без фильтра по номеру обслуживается прежним
``ix_inbound_sms_received_at``. Схема ``inbound_sms`` не меняется — только индекс.

``downgrade`` удаляет индекс (обратимо, данные не затрагиваются).

Revision ID: 20260703_004
Revises: 20260702_003
Create Date: 2026-07-03 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260703_004"
down_revision: str | None = "20260702_003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Raw SQL: DESC-порядок в выражении индекса (как ix_inbound_sms_received_at).
    op.execute(
        "CREATE INDEX ix_inbound_sms_to_number_received "
        "ON inbound_sms (to_number, received_at DESC, id DESC)"
    )


def downgrade() -> None:
    op.drop_index("ix_inbound_sms_to_number_received", table_name="inbound_sms")
