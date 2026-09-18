"""signal run and client signal tables

Revision ID: 7233423b4df3
Revises: 9532222e1bc2
Create Date: 2026-09-15 12:51:46.372509

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7233423b4df3"
down_revision: str | Sequence[str] | None = "9532222e1bc2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signal_run",
        sa.Column("run_id", sa.String(length=36), autoincrement=False, nullable=False),
        sa.Column("state", sa.String(length=16), server_default="running", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('running', 'completed', 'failed')", name="ck_signal_run_state"
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "client_signal_snapshot",
        sa.Column("snapshot_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.Column("signal_code", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["signal_run.run_id"]),
        sa.PrimaryKeyConstraint("snapshot_id"),
        sa.UniqueConstraint(
            "run_id",
            "client_id",
            "unit_fund_id",
            "signal_code",
            name="uq_client_signal_snapshot_run_client_fund_signal",
        ),
    )
    op.create_index(
        "ix_client_signal_snapshot_client_fund_signal_run",
        "client_signal_snapshot",
        ["client_id", "unit_fund_id", "signal_code", "run_id"],
        unique=False,
    )
    op.create_table(
        "client_signal_state",
        sa.Column("client_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("signal_code", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("since", sa.Date(), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["signal_run.run_id"]),
        sa.PrimaryKeyConstraint("client_id", "unit_fund_id", "signal_code"),
    )


def downgrade() -> None:
    op.drop_table("client_signal_state")
    op.drop_index(
        "ix_client_signal_snapshot_client_fund_signal_run", table_name="client_signal_snapshot"
    )
    op.drop_table("client_signal_snapshot")
    op.drop_table("signal_run")
