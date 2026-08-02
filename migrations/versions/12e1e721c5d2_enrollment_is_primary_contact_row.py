"""enrollment is_primary_contact_row

Revision ID: 12e1e721c5d2
Revises: 6459c8537ef7
Create Date: 2026-07-31 09:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "12e1e721c5d2"
down_revision: str | Sequence[str] | None = "6459c8537ef7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "enrollment",
        sa.Column(
            "is_primary_contact_row", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("enrollment", "is_primary_contact_row")
