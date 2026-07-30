"""campaign outreach_message review_action tables

Revision ID: 1b334ff8ae77
Revises: f71012028fc4
Create Date: 2026-07-30 11:51:27.182623

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "1b334ff8ae77"
down_revision: str | Sequence[str] | None = "f71012028fc4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign",
        sa.Column("campaign_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "campaign_type", sa.Text(), server_default="dormant_reengagement", nullable=False
        ),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'running', 'paused', 'completed')", name="ck_campaign_status"
        ),
        sa.PrimaryKeyConstraint("campaign_id"),
    )

    op.create_table(
        "outreach_message",
        sa.Column("message_id", sa.Text(), autoincrement=False, nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("generation_run_id", sa.Text(), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.Text(), server_default="email", nullable=False),
        sa.Column("ai_draft_content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("personalized_content", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending_review", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'escalated', 'held')",
            name="ck_outreach_message_status",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.campaign_id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.client_id"]),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.run_id"]),
        sa.PrimaryKeyConstraint("message_id"),
        sa.UniqueConstraint("generation_run_id"),
    )
    op.create_index("ix_outreach_message_campaign_id", "outreach_message", ["campaign_id"])
    op.create_index("ix_outreach_message_client_id", "outreach_message", ["client_id"])
    op.create_index("ix_outreach_message_status", "outreach_message", ["status"])

    op.create_table(
        "review_action",
        sa.Column("review_action_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("reviewer_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("edited_content", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('approve', 'edit_approve', 'reject', 'escalate', 'hold')",
            name="ck_review_action_outcome",
        ),
        sa.ForeignKeyConstraint(["message_id"], ["outreach_message.message_id"]),
        sa.PrimaryKeyConstraint("review_action_id"),
    )
    op.create_index("ix_review_action_message_id", "review_action", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_review_action_message_id", table_name="review_action")
    op.drop_table("review_action")
    op.drop_index("ix_outreach_message_status", table_name="outreach_message")
    op.drop_index("ix_outreach_message_client_id", table_name="outreach_message")
    op.drop_index("ix_outreach_message_campaign_id", table_name="outreach_message")
    op.drop_table("outreach_message")
    op.drop_table("campaign")
