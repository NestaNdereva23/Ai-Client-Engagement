from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c2e9f4b138"
down_revision: str | Sequence[str] | None = "d8a3f1c5b724"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("touch_plan", sa.Column("suggested_subject", sa.Text(), nullable=True))
    op.add_column("agent_run", sa.Column("plan_report", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_run", "plan_report")
    op.drop_column("touch_plan", "suggested_subject")
