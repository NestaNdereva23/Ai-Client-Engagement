from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6e1d4a9c372"
down_revision: str | Sequence[str] | None = "4a9811431f50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "action_result",
        sa.Column("result_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("proposal_id", sa.BigInteger(), nullable=False),
        sa.Column("insight_id", sa.BigInteger(), nullable=True),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("opened", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("replied", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("opted_out", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("bounced", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("reviewer_changed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("deposited", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("deposit_amount_kes", sa.Float(), server_default="0", nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("window_days > 0", name="ck_action_result_window_days_positive"),
        sa.CheckConstraint(
            "deposit_amount_kes >= 0", name="ck_action_result_deposit_amount_not_negative"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["agent_proposal.proposal_id"], name="fk_action_result_proposal_id"
        ),
        sa.ForeignKeyConstraint(
            ["insight_id"], ["agent_insight.insight_id"], name="fk_action_result_insight_id"
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["outreach_message.message_id"], name="fk_action_result_message_id"
        ),
        sa.PrimaryKeyConstraint("result_id"),
        sa.UniqueConstraint("message_id", "window_days", name="uq_action_result_message_window"),
    )
    op.create_index(op.f("ix_action_result_proposal_id"), "action_result", ["proposal_id"])
    op.create_index(op.f("ix_action_result_insight_id"), "action_result", ["insight_id"])
    op.create_index(op.f("ix_action_result_client_id"), "action_result", ["client_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_action_result_client_id"), table_name="action_result")
    op.drop_index(op.f("ix_action_result_insight_id"), table_name="action_result")
    op.drop_index(op.f("ix_action_result_proposal_id"), table_name="action_result")
    op.drop_table("action_result")
