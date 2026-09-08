"""One run of the agent loop, and the ordered trace of every tool it called.

agent_run is created when the loop starts and updated once it stops. It may
point at the risk_run whose scores tonight's groups came from. agent_tool_call
is kept apart from tool_calls, which is a drafting run's own trace, so each
table stays about exactly one kind of run. This migration also gives
agent_proposal.run_id the foreign key it was written without, back when
agent_run did not exist yet.

Revision ID: eb88fd1aa4fd
Revises: 6f3da7fbaf89
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "eb88fd1aa4fd"
down_revision: str | Sequence[str] | None = "6f3da7fbaf89"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_run",
        sa.Column("run_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.Text(), server_default="running", nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("risk_run_id", sa.String(length=36), nullable=True),
        sa.Column("plan_text", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("cost_kes", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "state IN ('running', 'completed', 'failed')", name="ck_agent_run_state"
        ),
        sa.CheckConstraint("trigger IN ('nightly', 'manual', 'chat')", name="ck_agent_run_trigger"),
        sa.CheckConstraint(
            "cost_kes IS NULL OR cost_kes >= 0", name="ck_agent_run_cost_not_negative"
        ),
        sa.ForeignKeyConstraint(
            ["risk_run_id"], ["risk_run.run_id"], name="fk_agent_run_risk_run_id"
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "agent_tool_call",
        sa.Column("tool_call_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("tool_input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tool_output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_run.run_id"], name="fk_agent_tool_call_run_id"),
        sa.PrimaryKeyConstraint("tool_call_id"),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_agent_tool_call_run_ordinal"),
    )
    op.create_index(op.f("ix_agent_tool_call_run_id"), "agent_tool_call", ["run_id"], unique=False)
    op.create_foreign_key(
        "fk_agent_proposal_run_id", "agent_proposal", "agent_run", ["run_id"], ["run_id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_agent_proposal_run_id", "agent_proposal", type_="foreignkey")
    op.drop_index(op.f("ix_agent_tool_call_run_id"), table_name="agent_tool_call")
    op.drop_table("agent_tool_call")
    op.drop_table("agent_run")
