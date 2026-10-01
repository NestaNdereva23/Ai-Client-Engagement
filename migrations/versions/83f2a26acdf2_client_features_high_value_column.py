"""client_features high_value column

Revision ID: 83f2a26acdf2
Revises: b7e3c9a1d5f2
Create Date: 2026-09-29 21:00:05.989323

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "83f2a26acdf2"
down_revision: str | Sequence[str] | None = "b7e3c9a1d5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "client_features",
        sa.Column("high_value", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.alter_column("client_features", "priority_tier", server_default="one_time_withdrawers")


def downgrade() -> None:
    op.alter_column("client_features", "priority_tier", server_default="T4")
    op.drop_column("client_features", "high_value")
