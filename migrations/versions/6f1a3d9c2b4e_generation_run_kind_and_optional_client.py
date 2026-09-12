from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6f1a3d9c2b4e"
down_revision: str | Sequence[str] | None = "12d3cd13bb04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generation_runs",
        sa.Column("run_kind", sa.Text(), nullable=False, server_default="production"),
    )
    op.create_check_constraint(
        "ck_generation_runs_run_kind",
        "generation_runs",
        "run_kind IN ('production', 'test')",
    )
    op.alter_column("generation_runs", "client_id", existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    op.alter_column("generation_runs", "client_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_constraint("ck_generation_runs_run_kind", "generation_runs", type_="check")
    op.drop_column("generation_runs", "run_kind")
