from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a5c9e2d7b1f3"
down_revision: str | Sequence[str] | None = "f2b8d6a4c9e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "observed_pattern",
        sa.Column("pattern_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("pattern_key", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("compared_with", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("outcome_percent", sa.Float(), nullable=False),
        sa.Column("comparison_percent", sa.Float(), nullable=False),
        sa.Column("gap_points", sa.Float(), nullable=False),
        sa.Column("sent_count", sa.Integer(), nullable=False),
        sa.Column("comparison_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default="new", nullable=False),
        sa.Column("insight_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "found_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('new', 'worth_a_group_filter', 'set_aside')",
            name="ck_observed_pattern_status",
        ),
        sa.CheckConstraint(
            "direction IN ('better', 'worse')", name="ck_observed_pattern_direction"
        ),
        sa.CheckConstraint(
            "outcome IN ('deposited', 'replied')", name="ck_observed_pattern_outcome"
        ),
        sa.CheckConstraint(
            "compared_with IN ('other_messages', 'not_messaged')",
            name="ck_observed_pattern_compared_with",
        ),
        sa.CheckConstraint("window_days > 0", name="ck_observed_pattern_window_days_positive"),
        sa.CheckConstraint("sent_count > 0", name="ck_observed_pattern_sent_count_positive"),
        sa.CheckConstraint("comparison_count > 0", name="ck_observed_pattern_comparison_positive"),
        sa.CheckConstraint(
            "(reviewed_by IS NULL) = (reviewed_at IS NULL)",
            name="ck_observed_pattern_review_pair",
        ),
        sa.ForeignKeyConstraint(["insight_id"], ["agent_insight.insight_id"]),
        sa.PrimaryKeyConstraint("pattern_id"),
        sa.UniqueConstraint("pattern_key", name="uq_observed_pattern_key"),
    )
    op.create_index("ix_observed_pattern_insight_id", "observed_pattern", ["insight_id"])


def downgrade() -> None:
    op.drop_index("ix_observed_pattern_insight_id", table_name="observed_pattern")
    op.drop_table("observed_pattern")
