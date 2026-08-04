"""client_features.priority_tier: derived, not rule-emitted

Revision ID: a1c7f3e9b6d2
Revises: f8b2e4a7c1d9
Create Date: 2026-08-03 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c7f3e9b6d2"
down_revision: str | Sequence[str] | None = "f8b2e4a7c1d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "client_features",
        sa.Column("priority_tier", sa.Text(), server_default="T4", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("client_features", "priority_tier")
