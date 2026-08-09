"""generation_batch and generation_batch_item tables

Revision ID: c4f8a1d3e7b2
Revises: a3681932b889
Create Date: 2026-08-08 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4f8a1d3e7b2"
down_revision: str | Sequence[str] | None = "a3681932b889"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generation_batch",
        sa.Column("generation_batch_id", sa.Text(), autoincrement=False, nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_batch_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="building"),
        sa.Column("requested_limit", sa.Integer(), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_count", sa.Integer(), nullable=True),
        sa.Column("errored_count", sa.Integer(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("generation_batch_id"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.campaign_id"]),
        sa.CheckConstraint(
            "status IN ('building', 'no_eligible_clients', 'submitted', 'in_progress', "
            "'ended', 'ingested', 'failed')",
            name="ck_generation_batch_status",
        ),
    )
    op.create_index(
        "ix_generation_batch_campaign_id", "generation_batch", ["campaign_id"], unique=False
    )
    op.create_index(
        "ix_generation_batch_provider_batch_id",
        "generation_batch",
        ["provider_batch_id"],
        unique=False,
    )

    op.create_table(
        "generation_batch_item",
        sa.Column("generation_batch_item_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("generation_batch_id", sa.Text(), nullable=False),
        sa.Column("custom_id", sa.Text(), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("enrollment_id", sa.BigInteger(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("context_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("generation_batch_item_id"),
        sa.ForeignKeyConstraint(["generation_batch_id"], ["generation_batch.generation_batch_id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.client_id"]),
        sa.ForeignKeyConstraint(["enrollment_id"], ["enrollment.enrollment_id"]),
        sa.UniqueConstraint(
            "generation_batch_id", "custom_id", name="uq_generation_batch_item_custom_id"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name="ck_generation_batch_item_status",
        ),
    )
    op.create_index(
        "ix_generation_batch_item_generation_batch_id",
        "generation_batch_item",
        ["generation_batch_id"],
        unique=False,
    )
    op.create_index(
        "ix_generation_batch_item_client_id", "generation_batch_item", ["client_id"], unique=False
    )
    op.create_index(
        "ix_generation_batch_item_enrollment_id",
        "generation_batch_item",
        ["enrollment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_generation_batch_item_enrollment_id", table_name="generation_batch_item")
    op.drop_index("ix_generation_batch_item_client_id", table_name="generation_batch_item")
    op.drop_index(
        "ix_generation_batch_item_generation_batch_id", table_name="generation_batch_item"
    )
    op.drop_table("generation_batch_item")

    op.drop_index("ix_generation_batch_provider_batch_id", table_name="generation_batch")
    op.drop_index("ix_generation_batch_campaign_id", table_name="generation_batch")
    op.drop_table("generation_batch")
