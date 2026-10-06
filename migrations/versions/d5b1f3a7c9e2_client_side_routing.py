"""client_side routing decision

Revision ID: d5b1f3a7c9e2
Revises: c3d4e5f6a7b8
Create Date: 2026-10-06 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5b1f3a7c9e2"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_side",
        sa.Column("client_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("balance", sa.Float(), nullable=False),
        sa.Column("threshold_kes", sa.Float(), nullable=False),
        sa.Column("source_feed", sa.Text(), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("client_id"),
        sa.CheckConstraint("side in ('active', 'inactive')", name="ck_client_side_side"),
    )


def downgrade() -> None:
    op.drop_table("client_side")
