"""fa_assignment table

Revision ID: dd2306473c5c
Revises: 8aaa8e4abb19
Create Date: 2026-08-10 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dd2306473c5c"
down_revision: str | Sequence[str] | None = "8aaa8e4abb19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fa_assignment",
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.Column("fa_id", sa.BigInteger(), nullable=True),
        sa.Column("fa_name", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), server_default="stub", nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("client_id", "unit_fund_id"),
    )
    # No grant to the model-facing role: fa_name is display only and never
    # sent to a model, same discipline as the other risk-side tables.


def downgrade() -> None:
    op.drop_table("fa_assignment")
