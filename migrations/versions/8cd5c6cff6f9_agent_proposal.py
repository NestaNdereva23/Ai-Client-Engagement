"""Hold one proposed action for one group of clients.

This is the record of what the agent wants to do tonight: the group, the
evidence, the reason, and the state it is in. It does not record the exact
list of clients, that is agent_proposal_client in the migration after this
one.

Revision ID: 8cd5c6cff6f9
Revises: c4e8a2d6f7b1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "8cd5c6cff6f9"
down_revision: str | Sequence[str] | None = "c4e8a2d6f7b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_proposal",
        sa.Column("proposal_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("action_code", sa.Text(), nullable=False),
        sa.Column("catalog_version", sa.Integer(), nullable=False),
        sa.Column("group_name", sa.Text(), nullable=False),
        sa.Column("group_definition", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("client_count", sa.Integer(), nullable=False),
        sa.Column("money_total_kes", sa.Float(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("angle", sa.Text(), nullable=True),
        sa.Column("content_mix", sa.Text(), nullable=True),
        sa.Column("variant", sa.Text(), nullable=True),
        sa.Column("permission_applied", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="proposed"),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("campaign_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'rejected', 'expired', 'approved', 'running', "
            "'blocked', 'sent', 'stopped', 'measured')",
            name="ck_agent_proposal_status",
        ),
        sa.CheckConstraint(
            "permission_applied IN ('suggest_only', 'approve_each', 'approve_sample', 'act_alone')",
            name="ck_agent_proposal_permission_applied",
        ),
        sa.CheckConstraint(
            "content_mix IS NULL OR content_mix IN "
            "('learning_only', 'mostly_learning', 'balanced', 'mostly_ask')",
            name="ck_agent_proposal_content_mix",
        ),
        sa.CheckConstraint("client_count >= 0", name="ck_agent_proposal_client_count_not_negative"),
        sa.CheckConstraint(
            "money_total_kes IS NULL OR money_total_kes >= 0",
            name="ck_agent_proposal_money_total_not_negative",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaign.campaign_id"], name="fk_agent_proposal_campaign_id"
        ),
        sa.PrimaryKeyConstraint("proposal_id"),
    )
    op.create_index(
        op.f("ix_agent_proposal_action_code"), "agent_proposal", ["action_code"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_proposal_action_code"), table_name="agent_proposal")
    op.drop_table("agent_proposal")
