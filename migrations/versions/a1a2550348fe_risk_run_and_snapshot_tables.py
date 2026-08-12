"""risk run and snapshot tables

Revision ID: a1a2550348fe
Revises: b6f3d8a1c4e7
Create Date: 2026-08-12 15:11:09.361502

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1a2550348fe"
down_revision: str | Sequence[str] | None = "b6f3d8a1c4e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "risk_run",
        sa.Column("run_id", sa.String(length=36), autoincrement=False, nullable=False),
        sa.Column("state", sa.String(length=16), server_default="running", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "reference_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("state IN ('running', 'completed', 'failed')", name="ck_risk_run_state"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "risk_snapshot",
        sa.Column("snapshot_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
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
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["risk_run.run_id"]),
        sa.PrimaryKeyConstraint("snapshot_id"),
        sa.UniqueConstraint(
            "run_id", "client_id", "unit_fund_id", name="uq_risk_snapshot_run_client_fund"
        ),
    )
    op.create_index(
        "ix_risk_snapshot_client_fund_run",
        "risk_snapshot",
        ["client_id", "unit_fund_id", "run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_risk_snapshot_client_fund_run", table_name="risk_snapshot")
    op.drop_table("risk_snapshot")
    op.drop_table("risk_run")
