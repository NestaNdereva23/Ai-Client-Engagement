from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4a7c2b9d513"
down_revision: str | Sequence[str] | None = "d3f8a1c5e9b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "action_performance",
        sa.Column("performance_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_hours", sa.Integer(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("action_code", sa.Text(), nullable=False),
        sa.Column("angle", sa.Text(), nullable=False),
        sa.Column("priority_tier", sa.Text(), nullable=False),
        sa.Column("risk_band", sa.Text(), nullable=False),
        sa.Column("content_mix", sa.Text(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=False),
        sa.Column("sent_count", sa.Integer(), nullable=False),
        sa.Column("replied_count", sa.Integer(), nullable=False),
        sa.Column("opted_out_count", sa.Integer(), nullable=False),
        sa.Column("edited_count", sa.Integer(), nullable=False),
        sa.Column("deposited_count", sa.Integer(), nullable=False),
        sa.Column("reply_rate", sa.Float(), nullable=False),
        sa.Column("opt_out_rate", sa.Float(), nullable=False),
        sa.Column("edit_rate", sa.Float(), nullable=False),
        sa.Column("deposit_rate", sa.Float(), nullable=False),
        sa.Column("money_in_kes", sa.Float(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("period_hours > 0", name="ck_action_performance_period_hours_positive"),
        sa.CheckConstraint("window_days > 0", name="ck_action_performance_window_days_positive"),
        sa.CheckConstraint("sent_count > 0", name="ck_action_performance_sent_count_positive"),
        sa.CheckConstraint("money_in_kes >= 0", name="ck_action_performance_money_not_negative"),
        sa.PrimaryKeyConstraint("performance_id"),
        sa.UniqueConstraint(
            "period_start",
            "period_hours",
            "window_days",
            "action_code",
            "angle",
            "priority_tier",
            "risk_band",
            "content_mix",
            "variant",
            name="uq_action_performance_period_key",
        ),
    )
    op.create_index(
        "ix_action_performance_read",
        "action_performance",
        ["period_hours", "window_days", "period_start"],
    )


def downgrade() -> None:
    op.drop_index("ix_action_performance_read", table_name="action_performance")
    op.drop_table("action_performance")
