"""client_features active_book_auto_checkin column

Revision ID: a1c5e9f3b7d2
Revises: d8f3a6c1b9e4
Create Date: 2026-08-24 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c5e9f3b7d2"
down_revision: str | Sequence[str] | None = "d8f3a6c1b9e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "client_features",
        sa.Column(
            "active_book_auto_checkin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("client_features", "active_book_auto_checkin")
