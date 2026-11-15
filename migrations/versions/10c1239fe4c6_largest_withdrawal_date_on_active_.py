"""largest withdrawal date on active client fund

Revision ID: 10c1239fe4c6
Revises: d4a7c1e9b562
Create Date: 2026-10-29 17:31:45.725037
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "10c1239fe4c6"
down_revision: str | Sequence[str] | None = "d4a7c1e9b562"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "active_client_fund", sa.Column("largest_withdrawal_date", sa.Date(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("active_client_fund", "largest_withdrawal_date")
