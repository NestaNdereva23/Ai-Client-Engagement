"""last real sale date on active client fund

Revision ID: 64e72b0c8350
Revises: f4b44c73b0a3
Create Date: 2026-08-12 16:22:21.682749

The most recent date among a client's real sales (excluding system fee
postings), independent of largest_real_sale -- the most recent real sale is
not necessarily the largest one. The on-demand briefing needs this to say
how long ago the largest visible redemption happened without re-flattening
raw_staging on every call.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "64e72b0c8350"
down_revision: str | Sequence[str] | None = "f4b44c73b0a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("active_client_fund", sa.Column("last_real_sale_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("active_client_fund", "last_real_sale_date")
