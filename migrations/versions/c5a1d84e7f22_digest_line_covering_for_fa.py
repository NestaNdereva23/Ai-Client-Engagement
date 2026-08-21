"""digest line covering_for_fa_id

Revision ID: c5a1d84e7f22
Revises: b3f7c2a9d15e
Create Date: 2026-08-19 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5a1d84e7f22"
down_revision: str | Sequence[str] | None = "b3f7c2a9d15e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("digest_line", sa.Column("covering_for_fa_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("digest_line", "covering_for_fa_id")
