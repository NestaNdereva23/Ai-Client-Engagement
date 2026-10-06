"""balance_band feature column

Revision ID: e6a2c8f4b1d7
Revises: d5b1f3a7c9e2
Create Date: 2026-10-06 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6a2c8f4b1d7"
down_revision: str | Sequence[str] | None = "d5b1f3a7c9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "client_features",
        sa.Column("balance_band", sa.Text(), nullable=False, server_default="nearly_empty"),
    )


def downgrade() -> None:
    op.drop_column("client_features", "balance_band")
