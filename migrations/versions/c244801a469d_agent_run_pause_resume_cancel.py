"""agent_run: pause, resume and cancel

Adds two columns a running nightly agent loop needs to be stoppable and
resumable: control_requested, the flag the loop checks between steps and
between groups inside propose, and choices, the choose step's output, kept
so a resumed run never calls the model a second time for it. plan_text
already existed and is reused as-is. Widens the state check constraint to
add paused and cancelled.

Revision ID: c244801a469d
Revises: 713a7c01269b
Create Date: 2026-10-20 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c244801a469d"
down_revision: str | Sequence[str] | None = "713a7c01269b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_run", sa.Column("control_requested", sa.Text(), nullable=True))
    op.add_column(
        "agent_run", sa.Column("choices", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_check_constraint(
        "ck_agent_run_control_requested",
        "agent_run",
        "control_requested IS NULL OR control_requested IN ('pause', 'cancel')",
    )
    op.drop_constraint("ck_agent_run_state", "agent_run", type_="check")
    op.create_check_constraint(
        "ck_agent_run_state",
        "agent_run",
        "state IN ('running', 'completed', 'failed', 'paused', 'cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_run_state", "agent_run", type_="check")
    op.create_check_constraint(
        "ck_agent_run_state", "agent_run", "state IN ('running', 'completed', 'failed')"
    )
    op.drop_constraint("ck_agent_run_control_requested", "agent_run", type_="check")
    op.drop_column("agent_run", "choices")
    op.drop_column("agent_run", "control_requested")
