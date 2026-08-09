"""message_template table; outreach_message.generation_run_id becomes many-to-one

Revision ID: d8a1f4c6e9b3
Revises: c4f8a1d3e7b2
Create Date: 2026-08-09 09:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d8a1f4c6e9b3"
down_revision: str | Sequence[str] | None = "c4f8a1d3e7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_template",
        sa.Column("template_id", sa.Text(), autoincrement=False, nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("generation_run_id", sa.Text(), nullable=False),
        sa.Column("profile_key", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ai_draft_content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            name="ck_message_template_status",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.campaign_id"]),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.run_id"]),
        sa.PrimaryKeyConstraint("template_id"),
        sa.UniqueConstraint("generation_run_id"),
    )
    op.create_index("ix_message_template_campaign_id", "message_template", ["campaign_id"])
    op.create_index("ix_message_template_status", "message_template", ["status"])

    op.drop_constraint("outreach_message_generation_run_id_key", "outreach_message", type_="unique")
    op.add_column("outreach_message", sa.Column("template_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "outreach_message_template_id_fkey",
        "outreach_message",
        "message_template",
        ["template_id"],
        ["template_id"],
    )
    op.create_index("ix_outreach_message_template_id", "outreach_message", ["template_id"])


def downgrade() -> None:
    op.drop_index("ix_outreach_message_template_id", table_name="outreach_message")
    op.drop_constraint("outreach_message_template_id_fkey", "outreach_message", type_="foreignkey")
    op.drop_column("outreach_message", "template_id")
    op.create_unique_constraint(
        "outreach_message_generation_run_id_key", "outreach_message", ["generation_run_id"]
    )

    op.drop_index("ix_message_template_status", table_name="message_template")
    op.drop_index("ix_message_template_campaign_id", table_name="message_template")
    op.drop_table("message_template")
