"""client_fund activity window and extended history flag

Revision ID: c8f14a6d92b7
Revises: b7d3f1a9c2e5
Create Date: 2026-09-22 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8f14a6d92b7"
down_revision: str | Sequence[str] | None = "b7d3f1a9c2e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "client_fund",
        sa.Column(
            "has_extended_history", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column("client_fund", sa.Column("activity_window_from", sa.Date(), nullable=True))
    op.add_column("client_fund", sa.Column("activity_window_to", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("client_fund", "activity_window_to")
    op.drop_column("client_fund", "activity_window_from")
    op.drop_column("client_fund", "has_extended_history")
