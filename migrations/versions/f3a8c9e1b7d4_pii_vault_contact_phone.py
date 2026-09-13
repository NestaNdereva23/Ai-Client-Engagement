"""pii_vault contact_phone

Revision ID: f3a8c9e1b7d4
Revises: a696124d74ab
Create Date: 2026-09-13 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a8c9e1b7d4"
down_revision: str | Sequence[str] | None = "a696124d74ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pii_vault", sa.Column("contact_phone", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("pii_vault", "contact_phone")
