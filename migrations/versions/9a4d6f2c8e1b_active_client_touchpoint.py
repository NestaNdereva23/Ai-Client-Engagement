from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9a4d6f2c8e1b"
down_revision: str | Sequence[str] | None = "c5e1a7d9b3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "active_client_touchpoint"
INDEX = "ix_active_client_touchpoint_client_fund_status"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("due_time", sa.Time(), nullable=True),
        sa.Column("minutes", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("done_by_interaction_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('call', 'meeting', 'email', 'reminder')",
            name="ck_active_client_touchpoint_kind",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'done', 'removed')", name="ck_active_client_touchpoint_status"
        ),
        sa.ForeignKeyConstraint(
            ["done_by_interaction_id"], ["active_client_interaction.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(INDEX, TABLE, ["client_id", "unit_fund_id", "status", "due_date"])


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
