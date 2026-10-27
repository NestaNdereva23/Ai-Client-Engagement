from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4a8d2c6b913"
down_revision: str | Sequence[str] | None = "e1b4f2a7c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KIND_CHECK = "agent_kind IN ('nightly', 'intelligence', 'action', 'chat')"
_NEW_KIND_CHECK = "agent_kind IN ('nightly', 'intelligence', 'action', 'chat', 'campaign')"


def upgrade() -> None:
    op.create_table(
        "touch_plan",
        sa.Column("plan_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("enrollment_id", sa.BigInteger(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("angle", sa.Text(), nullable=False),
        sa.Column("gap_days", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["enrollment_id"], ["enrollment.enrollment_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["agent_run.run_id"]),
        sa.PrimaryKeyConstraint("plan_id"),
        sa.UniqueConstraint("enrollment_id", "step_no", name="uq_touch_plan_enrollment_step"),
    )
    op.create_index(op.f("ix_touch_plan_enrollment_id"), "touch_plan", ["enrollment_id"])
    op.create_index(op.f("ix_touch_plan_run_id"), "touch_plan", ["run_id"])

    op.add_column(
        "campaign",
        sa.Column("agent_touch_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.drop_constraint("ck_agent_run_agent_kind", "agent_run", type_="check")
    op.create_check_constraint("ck_agent_run_agent_kind", "agent_run", _NEW_KIND_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_agent_run_agent_kind", "agent_run", type_="check")
    op.create_check_constraint("ck_agent_run_agent_kind", "agent_run", _OLD_KIND_CHECK)

    op.drop_column("campaign", "agent_touch_policy")

    op.drop_index(op.f("ix_touch_plan_run_id"), table_name="touch_plan")
    op.drop_index(op.f("ix_touch_plan_enrollment_id"), table_name="touch_plan")
    op.drop_table("touch_plan")
