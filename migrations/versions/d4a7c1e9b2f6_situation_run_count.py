from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a7c1e9b2f6"
down_revision: str | Sequence[str] | None = "c8f14a6d92b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "situation_run_count",
        sa.Column("run_id", sa.String(length=36), autoincrement=False, nullable=False),
        sa.Column("situation_code", sa.Text(), nullable=False),
        sa.Column("active_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["signal_run.run_id"]),
        sa.PrimaryKeyConstraint("run_id", "situation_code"),
    )
    op.execute(
        """
        INSERT INTO situation_run_count (run_id, situation_code, active_count)
        SELECT run_id, situation_code, count(*) FILTER (WHERE is_active)
        FROM client_situation_snapshot
        GROUP BY run_id, situation_code
        """
    )


def downgrade() -> None:
    op.drop_table("situation_run_count")
