"""has_balance feature column

Revision ID: b3e7f2a9c4d6
Revises: a7f3d9c2e5b8
Create Date: 2026-10-06 13:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3e7f2a9c4d6"
down_revision: str | Sequence[str] | None = "a7f3d9c2e5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "client_features",
        sa.Column("has_balance", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("client_features", "has_balance")
