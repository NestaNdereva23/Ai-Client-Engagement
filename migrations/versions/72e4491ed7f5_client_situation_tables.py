"""client situation tables

Revision ID: 72e4491ed7f5
Revises: 7233423b4df3
Create Date: 2026-09-15 13:21:20.097712

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "72e4491ed7f5"
down_revision: str | Sequence[str] | None = "7233423b4df3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_situation_snapshot",
        sa.Column("snapshot_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.Column("situation_code", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("signal_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            "situation_code",
            name="uq_client_situation_snapshot_run_client_fund_situation",
        ),
    )
    op.create_index(
        "ix_client_situation_snapshot_client_fund_situation_run",
        "client_situation_snapshot",
        ["client_id", "unit_fund_id", "situation_code", "run_id"],
        unique=False,
    )
    op.create_table(
        "client_situation_state",
        sa.Column("client_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("situation_code", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("signal_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("since", sa.Date(), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["signal_run.run_id"]),
        sa.PrimaryKeyConstraint("client_id", "unit_fund_id", "situation_code"),
    )


def downgrade() -> None:
    op.drop_table("client_situation_state")
    op.drop_index(
        "ix_client_situation_snapshot_client_fund_situation_run",
        table_name="client_situation_snapshot",
    )
    op.drop_table("client_situation_snapshot")
