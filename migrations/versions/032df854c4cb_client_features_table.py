"""client_features table

Revision ID: 032df854c4cb
Revises: ad044d684702
Create Date: 2026-07-23 11:02:25.079860

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "032df854c4cb"
down_revision: str | Sequence[str] | None = "ad044d684702"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_features",
        sa.Column("client_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("archetype", sa.Text(), nullable=False),
        sa.Column("recency_bucket", sa.Text(), nullable=False),
        sa.Column("value_tier", sa.Text(), nullable=False),
        sa.Column("own_rhythm_days", sa.Integer(), nullable=True),
        sa.Column("rhythm_band", sa.Text(), nullable=False),
        sa.Column("observed_volume", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "purchases_censored", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "history_censored", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.client_id"]),
        sa.PrimaryKeyConstraint("client_id"),
    )


def downgrade() -> None:
    op.drop_table("client_features")
