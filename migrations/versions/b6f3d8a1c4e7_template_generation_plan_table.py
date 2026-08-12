"""template_generation_plan table

Revision ID: b6f3d8a1c4e7
Revises: a4f8c2e91b07
Create Date: 2026-08-12 09:30:00.000000

One row per draft_templates_for_campaign call: estimated_templates,
effective_limit, drafted_count, skipped_existing, failed_guardrails, and
the policy values that produced effective_limit, copied rather than
referenced so an old plan stays explainable even after the policy changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6f3d8a1c4e7"
down_revision: str | Sequence[str] | None = "a4f8c2e91b07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "template_generation_plan",
        sa.Column("plan_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("estimated_templates", sa.Integer(), nullable=False),
        sa.Column("effective_limit", sa.Integer(), nullable=True),
        sa.Column("drafted_count", sa.Integer(), nullable=False),
        sa.Column("skipped_existing", sa.Integer(), nullable=False),
        sa.Column("failed_guardrails", sa.Integer(), nullable=False),
        sa.Column("policy_source", sa.Text(), nullable=False),
        sa.Column("policy_max_templates", sa.Integer(), nullable=True),
        sa.Column("policy_max_templates_pct", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("estimated_templates >= 0", name="ck_tgp_estimated_nonneg"),
        sa.CheckConstraint(
            "effective_limit IS NULL OR effective_limit >= 0",
            name="ck_tgp_effective_limit_nonneg",
        ),
        sa.CheckConstraint("drafted_count >= 0", name="ck_tgp_drafted_nonneg"),
        sa.CheckConstraint("skipped_existing >= 0", name="ck_tgp_skipped_nonneg"),
        sa.CheckConstraint("failed_guardrails >= 0", name="ck_tgp_failed_nonneg"),
        sa.CheckConstraint("policy_source IN ('campaign', 'default')", name="ck_tgp_policy_source"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.campaign_id"]),
        sa.PrimaryKeyConstraint("plan_id"),
    )
    op.create_index(
        "ix_template_generation_plan_campaign_id",
        "template_generation_plan",
        ["campaign_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_template_generation_plan_campaign_id", table_name="template_generation_plan")
    op.drop_table("template_generation_plan")
