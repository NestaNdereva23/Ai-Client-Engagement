"""Hold the ordered record of what happened during one agent run.

One row per thing worth showing a person, in the order it happened, so
watching a run live and opening a finished one are the same view over the
same rows. The place in the queue is unique within a run, so two events
never claim the same one even when groups are worked side by side.

Revision ID: b4d81e05c9a3
Revises: a7c2e91d4f60
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b4d81e05c9a3"
down_revision: str | Sequence[str] | None = "a7c2e91d4f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
    "run_started",
    "run_status",
    "run_completed",
    "step_started",
    "step_completed",
    "tool_started",
    "tool_completed",
    "insight_created",
    "insight_updated",
    "insight_dismissed",
    "proposal_created",
    "approval_needed",
    "approval_given",
    "action_started",
    "action_completed",
    "warning",
    "error",
    "paused",
    "resumed",
)

KIND_CHECK = "kind IN ({})".format(", ".join(f"'{kind}'" for kind in KINDS))


def upgrade() -> None:
    op.create_table(
        "agent_event",
        sa.Column("event_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(KIND_CHECK, name="ck_agent_event_kind"),
        sa.CheckConstraint("ordinal > 0", name="ck_agent_event_ordinal_positive"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_run.run_id"],
            name="fk_agent_event_run_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_agent_event_run_ordinal"),
    )
    op.create_index(op.f("ix_agent_event_run_id"), "agent_event", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_event_run_id"), table_name="agent_event")
    op.drop_table("agent_event")
