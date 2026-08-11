"""client risk features table

Revision ID: b3ef3bf2c1ea
Revises: 699b312a4439
Create Date: 2026-08-11 13:22:22.740619

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3ef3bf2c1ea"
down_revision: str | Sequence[str] | None = "699b312a4439"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_risk_features",
        sa.Column("client_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("recency_band", sa.Text(), nullable=True),
        sa.Column("balance_tier", sa.Text(), nullable=True),
        sa.Column("value_tier", sa.Text(), nullable=True),
        sa.Column("credible_rhythm", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("lapse_ratio", sa.Float(), nullable=True),
        sa.Column("sig_drawdown", sa.Boolean(), nullable=False),
        sa.Column("sig_dormant", sa.Boolean(), nullable=False),
        sa.Column("sig_cadence_break", sa.Boolean(), nullable=False),
        sa.Column("sig_shrinking", sa.Boolean(), nullable=False),
        sa.Column("sig_fee_erosion", sa.Boolean(), nullable=False),
        sa.Column("sig_never_repeated", sa.Boolean(), nullable=False),
        sa.Column("risk_score", sa.Integer(), nullable=False),
        sa.Column("risk_band", sa.Text(), nullable=False),
        sa.Column("risk_reasons", sa.Text(), nullable=False),
        sa.Column("aum_at_risk", sa.Float(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("route", sa.Text(), nullable=True),
        sa.Column("queue_rank", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("client_id", "unit_fund_id"),
    )


def downgrade() -> None:
    op.drop_table("client_risk_features")
