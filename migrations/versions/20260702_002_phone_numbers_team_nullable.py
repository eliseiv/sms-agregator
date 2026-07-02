"""phone_numbers.team_id → NULLABLE + FK ON DELETE SET NULL (ADR-0009).

Unassigned-номера: ``team_id IS NULL`` — легитимный пул. FK пересоздаётся с
``ON DELETE SET NULL`` (вместо ``ON DELETE CASCADE``), поэтому удаление команды
не уничтожает её номера, а возвращает их в пул. Индекс ``ix_phone_numbers_team_id``
остаётся непартиальным (обслуживает и assigned, и ``team_id IS NULL``).

``downgrade`` возвращает ``NOT NULL`` + ``ON DELETE CASCADE``. При наличии строк
с ``team_id IS NULL`` откат упадёт на ``SET NOT NULL`` — ожидаемо, применим только
на пустом пуле (docs/04-data-model.md §phone_numbers).

Revision ID: 20260702_002
Revises: 20260701_001
Create Date: 2026-07-02 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260702_002"
down_revision: str | None = "20260701_001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("fk_phone_numbers_team_id", "phone_numbers", type_="foreignkey")
    op.alter_column("phone_numbers", "team_id", nullable=True)
    op.create_foreign_key(
        "fk_phone_numbers_team_id",
        "phone_numbers",
        "teams",
        ["team_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_phone_numbers_team_id", "phone_numbers", type_="foreignkey")
    # Упадёт при наличии team_id IS NULL строк — откат применим только на пустом пуле.
    op.alter_column("phone_numbers", "team_id", nullable=False)
    op.create_foreign_key(
        "fk_phone_numbers_team_id",
        "phone_numbers",
        "teams",
        ["team_id"],
        ["id"],
        ondelete="CASCADE",
    )
