from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3b7d2a9c614"
down_revision: str | Sequence[str] | None = "e5a1c8d3f702"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("touch_plan", sa.Column("next_time_note", sa.Text(), nullable=True))
    op.add_column("touch_log", sa.Column("side_at_send", sa.Text(), nullable=True))
    op.add_column("touch_log", sa.Column("balance_at_send", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("touch_log", "balance_at_send")
    op.drop_column("touch_log", "side_at_send")
    op.drop_column("touch_plan", "next_time_note")
