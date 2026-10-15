"""user_access: advisor name and daily capacity, so the roster setting can go"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d2f4a8c1e3"
down_revision: str | Sequence[str] | None = "a9c3e7f1b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_access", sa.Column("name", sa.Text(), nullable=True))
    op.add_column(
        "user_access",
        sa.Column("daily_capacity", sa.Integer(), server_default="19", nullable=False),
    )
    op.execute(
        "UPDATE user_access u SET name = feed.name FROM ("
        "SELECT lower(btrim(fa_email)) AS email, max(fa_name) AS name "
        "FROM active_client_fund WHERE fa_email IS NOT NULL AND btrim(fa_email) <> '' "
        "GROUP BY 1) feed "
        "WHERE feed.email = u.email AND u.name IS NULL AND feed.name IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("user_access", "daily_capacity")
    op.drop_column("user_access", "name")
