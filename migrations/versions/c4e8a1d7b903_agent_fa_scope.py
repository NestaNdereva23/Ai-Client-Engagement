"""agent_run, agent_insight and agent_proposal: the advisor a scoped run was for"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a1d7b903"
down_revision: str | Sequence[str] | None = "b7d2f4a8c1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("agent_run", "agent_insight", "agent_proposal")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("fa_id", sa.Text(), nullable=True))
        op.create_index(f"ix_{table}_fa_id", table, ["fa_id"])


def downgrade() -> None:
    for table in TABLES:
        op.drop_index(f"ix_{table}_fa_id", table_name=table)
        op.drop_column(table, "fa_id")
