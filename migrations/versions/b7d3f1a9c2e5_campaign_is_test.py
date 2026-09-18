"""campaign is_test

Revision ID: b7d3f1a9c2e5
Revises: a4c8e2f61b93
Create Date: 2026-09-18 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d3f1a9c2e5"
down_revision: str | Sequence[str] | None = "a4c8e2f61b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("is_test", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("campaign", "is_test")
