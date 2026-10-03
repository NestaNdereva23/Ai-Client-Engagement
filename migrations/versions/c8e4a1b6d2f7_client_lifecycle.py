from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8e4a1b6d2f7"
down_revision: str | Sequence[str] | None = "a5c9e2d7b1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATE_LIST = "('active', 'dormant')"


def upgrade() -> None:
    policy = op.create_table(
        "lifecycle_policy",
        sa.Column("policy_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("policy_code", sa.Text(), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=False),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column("changed_reason", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(f"from_state IN {STATE_LIST}", name="ck_lifecycle_policy_from_state"),
        sa.CheckConstraint(f"to_state IN {STATE_LIST}", name="ck_lifecycle_policy_to_state"),
        sa.CheckConstraint("from_state <> to_state", name="ck_lifecycle_policy_a_real_change"),
        sa.CheckConstraint(
            "mode IN ('automatic', 'needs_a_person')", name="ck_lifecycle_policy_mode"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'object' AND evidence <> '{}'::jsonb",
            name="ck_lifecycle_policy_evidence_required",
        ),
        sa.PrimaryKeyConstraint("policy_id"),
        sa.UniqueConstraint("policy_code", name="uq_lifecycle_policy_code"),
        sa.UniqueConstraint("from_state", "to_state", name="uq_lifecycle_policy_change"),
    )
    op.create_table(
        "client_lifecycle",
        sa.Column("client_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("policy_code", sa.Text(), nullable=False),
        sa.Column("insight_id", sa.BigInteger(), nullable=True),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column(
            "changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(f"state IN {STATE_LIST}", name="ck_client_lifecycle_state"),
        sa.ForeignKeyConstraint(["insight_id"], ["agent_insight.insight_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("client_id", "unit_fund_id"),
    )
    op.create_index("ix_client_lifecycle_insight_id", "client_lifecycle", ["insight_id"])
    op.create_table(
        "agent_insight_lifecycle",
        sa.Column("insight_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("from_state", sa.Text(), nullable=False),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), server_default="pending", nullable=False),
        sa.Column("policy_code", sa.Text(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"from_state IN {STATE_LIST}", name="ck_insight_lifecycle_from_state"),
        sa.CheckConstraint(f"to_state IN {STATE_LIST}", name="ck_insight_lifecycle_to_state"),
        sa.CheckConstraint("from_state <> to_state", name="ck_insight_lifecycle_a_real_change"),
        sa.CheckConstraint(
            "outcome IN ('pending', 'no_policy', 'waiting_for_a_person', "
            "'evidence_not_met', 'applied')",
            name="ck_insight_lifecycle_outcome",
        ),
        sa.ForeignKeyConstraint(["insight_id"], ["agent_insight.insight_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("insight_id"),
    )
    op.bulk_insert(
        policy,
        [
            {
                "policy_code": "active_to_dormant",
                "from_state": "active",
                "to_state": "dormant",
                "evidence": {"conditions": [{"field": "sig_dormant", "op": "is_true"}]},
                "mode": "needs_a_person",
                "is_active": True,
                "description": (
                    "A client with no deposit in 12 months moves from active to dormant, "
                    "once a person agrees."
                ),
                "changed_by": "migration",
                "changed_reason": "First written rule. It waits for a person until someone "
                "decides it can act alone.",
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("agent_insight_lifecycle")
    op.drop_index("ix_client_lifecycle_insight_id", table_name="client_lifecycle")
    op.drop_table("client_lifecycle")
    op.drop_table("lifecycle_policy")
