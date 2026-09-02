"""instantiation_batch table

Revision ID: a7c2e5f9b1d4
Revises: ec61768b425d
Create Date: 2026-09-01 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c2e5f9b1d4"
down_revision: str | Sequence[str] | None = "ec61768b425d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instantiation_batch",
        sa.Column("instantiation_batch_id", sa.Text(), autoincrement=False, nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("template_count", sa.Integer(), nullable=False),
        sa.Column("instantiated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_template_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("instantiation_batch_id"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.campaign_id"]),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'no_approved_templates', 'failed')",
            name="ck_instantiation_batch_status",
        ),
    )
    op.create_index(
        "ix_instantiation_batch_campaign_id", "instantiation_batch", ["campaign_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_instantiation_batch_campaign_id", table_name="instantiation_batch")
    op.drop_table("instantiation_batch")
