"""digest line batch

Revision ID: e8a3f26b9c41
Revises: d71b93c4a6e0
Create Date: 2026-08-19 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8a3f26b9c41"
down_revision: str | Sequence[str] | None = "d71b93c4a6e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "digest_line",
        sa.Column("batch", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("digest_line", "batch")
