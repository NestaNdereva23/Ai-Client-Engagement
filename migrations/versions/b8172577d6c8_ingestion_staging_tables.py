"""ingestion staging tables

Revision ID: b8172577d6c8
Revises: 6988eb33281c
Create Date: 2026-07-22 10:54:40.908328

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b8172577d6c8"
down_revision: str | Sequence[str] | None = "6988eb33281c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_staging",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("natural_key", sa.Text(), nullable=False),
        sa.Column(
            "pulled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "natural_key", name="uq_raw_staging_run_natural"),
    )
    op.create_index(op.f("ix_raw_staging_run_id"), "raw_staging", ["run_id"], unique=False)

    op.create_table(
        "ingestion_status",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("fund_cursor", sa.Text(), nullable=True),
        sa.Column("page_cursor", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="running", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("records_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("records_written", sa.Integer(), server_default="0", nullable=False),
        sa.Column("records_rejected", sa.Integer(), server_default="0", nullable=False),
        sa.Column("shortfall", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "state IN ('running', 'completed', 'failed')",
            name="ck_ingestion_status_state",
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )

    op.create_table(
        "ingestion_rejects",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("raw_fragment", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ingestion_rejects_run_id"), "ingestion_rejects", ["run_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ingestion_rejects_run_id"), table_name="ingestion_rejects")
    op.drop_table("ingestion_rejects")
    op.drop_table("ingestion_status")
    op.drop_index(op.f("ix_raw_staging_run_id"), table_name="raw_staging")
    op.drop_table("raw_staging")
