"""review cohort sampling

Revision ID: f1a9c3e7b2d4
Revises: a7c4e91f2b6d
Create Date: 2026-08-19 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a9c3e7b2d4"
down_revision: str | Sequence[str] | None = "a7c4e91f2b6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_cohort",
        sa.Column("cohort_id", sa.Text(), nullable=False),
        sa.Column(
            "campaign_id",
            sa.BigInteger(),
            sa.ForeignKey("campaign.campaign_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("priority_tier", sa.Text(), nullable=False),
        # Null means every message in this cohort must be reviewed (no
        # sampling). A rate is the share of the cohort to sample, resolved
        # from the tier contract at creation time, with sample_cap as the
        # ceiling on how many that can come to.
        sa.Column("sample_rate", sa.Float(), nullable=True),
        sa.Column("sample_cap", sa.Integer(), nullable=True),
        sa.Column("assigned_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.Text(), nullable=False, server_default="sampling"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("cohort_id"),
        sa.UniqueConstraint("campaign_id", "priority_tier", name="uq_review_cohort_campaign_tier"),
        sa.CheckConstraint(
            "status IN ('sampling', 'ready_to_approve_rest', 'completed')",
            name="ck_review_cohort_status",
        ),
    )
    op.create_index("ix_review_cohort_campaign_id", "review_cohort", ["campaign_id"])

    op.add_column(
        "outreach_message",
        sa.Column(
            "cohort_id",
            sa.Text(),
            sa.ForeignKey("review_cohort.cohort_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_outreach_message_cohort_id", "outreach_message", ["cohort_id"])
    op.add_column(
        "outreach_message",
        sa.Column("is_sample", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    # Nullable, same "no row / no value means mandatory review" convention
    # tier_contract.human_approval already relies on -- see
    # app.rules.tier_contract.cohort_sample_rate_for. A team changes the
    # per-tier rates by adding a new tier_contract version through the
    # existing versioning path, not by editing this migration later.
    op.add_column("tier_contract", sa.Column("cohort_sample_rate", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("tier_contract", "cohort_sample_rate")
    op.drop_column("outreach_message", "is_sample")
    op.drop_index("ix_outreach_message_cohort_id", table_name="outreach_message")
    op.drop_column("outreach_message", "cohort_id")
    op.drop_index("ix_review_cohort_campaign_id", table_name="review_cohort")
    op.drop_table("review_cohort")
