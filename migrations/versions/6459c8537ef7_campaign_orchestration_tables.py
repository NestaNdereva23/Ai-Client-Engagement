"""campaign orchestration tables

Revision ID: 6459c8537ef7
Revises: 93a7ef0289bd
Create Date: 2026-07-31 08:15:26.926557

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "6459c8537ef7"
down_revision: str | Sequence[str] | None = "93a7ef0289bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# contact_events is compliance signal about a client, the same as suppression
# (CLAUDE.md §7): restricted role only, never the safe model-facing path.
RESTRICTED = "ace_restricted"
SAFE = "ace_safe"


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("cohort_definition", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("campaign", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("campaign", sa.Column("end_date", sa.Date(), nullable=True))

    op.create_table(
        "campaign_step",
        sa.Column("step_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("offset_days", sa.Integer(), nullable=False),
        sa.Column("message_angle", sa.Text(), nullable=False),
        sa.Column("template_ref", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.campaign_id"]),
        sa.PrimaryKeyConstraint("step_id"),
        sa.UniqueConstraint("campaign_id", "step_no", name="uq_campaign_step_no"),
    )
    op.create_index("ix_campaign_step_campaign_id", "campaign_step", ["campaign_id"])

    op.create_table(
        "enrollment",
        sa.Column("enrollment_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("current_step", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), server_default="enrolled", nullable=False),
        sa.Column(
            "enrolled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('enrolled', 'in_progress', 'excluded', 'completed', "
            "'stopped_reply', 'stopped_optout', 'stopped_bounce', 'stopped_reengaged')",
            name="ck_enrollment_status",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.campaign_id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.client_id"]),
        sa.PrimaryKeyConstraint("enrollment_id"),
        sa.UniqueConstraint("campaign_id", "client_id", name="uq_enrollment_campaign_client"),
    )
    op.create_index("ix_enrollment_campaign_id", "enrollment", ["campaign_id"])
    op.create_index("ix_enrollment_client_id", "enrollment", ["client_id"])

    op.create_table(
        "touch_log",
        sa.Column("touch_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("enrollment_id", sa.BigInteger(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_status", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["enrollment_id"], ["enrollment.enrollment_id"]),
        sa.ForeignKeyConstraint(["message_id"], ["outreach_message.message_id"]),
        sa.PrimaryKeyConstraint("touch_id"),
        sa.UniqueConstraint("enrollment_id", "step_no", name="uq_touch_log_enrollment_step"),
    )
    op.create_index("ix_touch_log_enrollment_id", "touch_log", ["enrollment_id"])

    op.create_table(
        "contact_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "type IN ('reply', 'open', 'bounce', 'complaint', 'optout')",
            name="ck_contact_events_type",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contact_events_client_id", "contact_events", ["client_id"])

    op.execute("REVOKE ALL ON contact_events FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON contact_events TO {RESTRICTED}")
    op.execute(f"REVOKE ALL ON contact_events FROM {SAFE}")


def downgrade() -> None:
    op.drop_index("ix_contact_events_client_id", table_name="contact_events")
    op.drop_table("contact_events")

    op.drop_index("ix_touch_log_enrollment_id", table_name="touch_log")
    op.drop_table("touch_log")

    op.drop_index("ix_enrollment_client_id", table_name="enrollment")
    op.drop_index("ix_enrollment_campaign_id", table_name="enrollment")
    op.drop_table("enrollment")

    op.drop_index("ix_campaign_step_campaign_id", table_name="campaign_step")
    op.drop_table("campaign_step")

    op.drop_column("campaign", "end_date")
    op.drop_column("campaign", "start_date")
    op.drop_column("campaign", "cohort_definition")
