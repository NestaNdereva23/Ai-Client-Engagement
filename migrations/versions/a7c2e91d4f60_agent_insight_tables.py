"""Hold what the agent found, the facts behind it, and who it is about.

An insight is the finding itself and what the model reads into it.
agent_insight_fact keeps each number apart from that reading, with the
filter and table it came from, so a figure can be run again and checked.
agent_insight_client is the exact list of client funds the finding covers.
agent_proposal gains insight_id so every proposal points at the finding
behind it.

Revision ID: a7c2e91d4f60
Revises: 3d51f82b8f3b
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a7c2e91d4f60"
down_revision: str | Sequence[str] | None = "3d51f82b8f3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_insight",
        sa.Column("insight_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("group_name", sa.Text(), nullable=False),
        sa.Column("group_definition", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("client_count", sa.Integer(), nullable=False),
        sa.Column("money_total_kes", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Text(), nullable=False),
        sa.Column("confidence_reason", sa.Text(), nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=False),
        sa.Column("avoid_saying", sa.Text(), nullable=True),
        sa.Column("why_now", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="new", nullable=False),
        sa.Column("dismissed_reason", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('risk', 'opportunity', 'lifecycle_change', 'anomaly', 'pattern', 'campaign')",
            name="ck_agent_insight_kind",
        ),
        sa.CheckConstraint(
            "state IN ('new', 'accepted', 'acted_on', 'dismissed', 'expired')",
            name="ck_agent_insight_state",
        ),
        sa.CheckConstraint(
            "confidence IN ('high', 'medium', 'low')",
            name="ck_agent_insight_confidence",
        ),
        sa.CheckConstraint(
            "state <> 'dismissed' OR dismissed_reason IS NOT NULL",
            name="ck_agent_insight_dismissed_reason_required",
        ),
        sa.CheckConstraint("client_count >= 0", name="ck_agent_insight_client_count_not_negative"),
        sa.CheckConstraint(
            "money_total_kes IS NULL OR money_total_kes >= 0",
            name="ck_agent_insight_money_total_not_negative",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_run.run_id"], name="fk_agent_insight_run_id"),
        sa.PrimaryKeyConstraint("insight_id"),
    )
    op.create_index(op.f("ix_agent_insight_run_id"), "agent_insight", ["run_id"], unique=False)
    op.create_index(op.f("ix_agent_insight_kind"), "agent_insight", ["kind"], unique=False)

    op.create_table(
        "agent_insight_fact",
        sa.Column("fact_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("insight_id", sa.BigInteger(), nullable=False),
        sa.Column("fact_text", sa.Text(), nullable=False),
        sa.Column("fact_value", sa.Text(), nullable=False),
        sa.Column("source_filter", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_table", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(source_filter) = 'object' AND source_filter <> '{}'::jsonb",
            name="ck_agent_insight_fact_source_filter_required",
        ),
        sa.CheckConstraint(
            "length(btrim(source_table)) > 0",
            name="ck_agent_insight_fact_source_table_required",
        ),
        sa.ForeignKeyConstraint(
            ["insight_id"],
            ["agent_insight.insight_id"],
            name="fk_agent_insight_fact_insight_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("fact_id"),
    )
    op.create_index(
        op.f("ix_agent_insight_fact_insight_id"),
        "agent_insight_fact",
        ["insight_id"],
        unique=False,
    )

    op.create_table(
        "agent_insight_client",
        sa.Column("insight_client_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("insight_id", sa.BigInteger(), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["insight_id"],
            ["agent_insight.insight_id"],
            name="fk_agent_insight_client_insight_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("insight_client_id"),
        sa.UniqueConstraint(
            "insight_id",
            "client_id",
            "unit_fund_id",
            name="uq_agent_insight_client_insight_client_fund",
        ),
    )
    op.create_index(
        op.f("ix_agent_insight_client_insight_id"),
        "agent_insight_client",
        ["insight_id"],
        unique=False,
    )

    op.add_column("agent_proposal", sa.Column("insight_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_agent_proposal_insight_id",
        "agent_proposal",
        "agent_insight",
        ["insight_id"],
        ["insight_id"],
    )
    op.create_index(
        op.f("ix_agent_proposal_insight_id"), "agent_proposal", ["insight_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_proposal_insight_id"), table_name="agent_proposal")
    op.drop_constraint("fk_agent_proposal_insight_id", "agent_proposal", type_="foreignkey")
    op.drop_column("agent_proposal", "insight_id")

    op.drop_index(op.f("ix_agent_insight_client_insight_id"), table_name="agent_insight_client")
    op.drop_table("agent_insight_client")

    op.drop_index(op.f("ix_agent_insight_fact_insight_id"), table_name="agent_insight_fact")
    op.drop_table("agent_insight_fact")

    op.drop_index(op.f("ix_agent_insight_kind"), table_name="agent_insight")
    op.drop_index(op.f("ix_agent_insight_run_id"), table_name="agent_insight")
    op.drop_table("agent_insight")
