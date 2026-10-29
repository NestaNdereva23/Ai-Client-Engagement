from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8a3f1c5b724"
down_revision: str | Sequence[str] | None = "b9e4a7c2d516"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("action_result", "proposal_id", existing_type=sa.BigInteger(), nullable=True)
    op.alter_column("action_result", "unit_fund_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column("action_result", sa.Column("touch_id", sa.BigInteger(), nullable=True))
    op.add_column("action_result", sa.Column("moved_to_active", sa.Boolean(), nullable=True))
    op.add_column("action_result", sa.Column("balance_up", sa.Boolean(), nullable=True))
    op.create_foreign_key(
        "fk_action_result_touch_id",
        "action_result",
        "touch_log",
        ["touch_id"],
        ["touch_id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_action_result_touch_id"), "action_result", ["touch_id"])
    op.create_check_constraint(
        "ck_action_result_has_a_source",
        "action_result",
        "proposal_id IS NOT NULL OR touch_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_action_result_has_a_source", "action_result", type_="check")
    op.execute("DELETE FROM action_result WHERE proposal_id IS NULL")
    op.drop_index(op.f("ix_action_result_touch_id"), table_name="action_result")
    op.drop_constraint("fk_action_result_touch_id", "action_result", type_="foreignkey")
    op.drop_column("action_result", "balance_up")
    op.drop_column("action_result", "moved_to_active")
    op.drop_column("action_result", "touch_id")
    op.alter_column("action_result", "unit_fund_id", existing_type=sa.BigInteger(), nullable=False)
    op.alter_column("action_result", "proposal_id", existing_type=sa.BigInteger(), nullable=False)
