"""client_tier previous_tier and tier_changed_at
Lets a nightly run tell which clients changed tier since the run before."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1c6a8f3b5d9"
down_revision: str | Sequence[str] | None = "d8f1b3a5c7e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "client_tier"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("previous_tier", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("tier_changed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column(TABLE, "tier_changed_at")
    op.drop_column(TABLE, "previous_tier")
