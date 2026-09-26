"""active_client_fund advisor name and email

Revision ID: e1b6d3a8c4f2
Revises: d4a7c1e9b2f6
Create Date: 2026-09-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1b6d3a8c4f2"
down_revision: str | Sequence[str] | None = "d4a7c1e9b2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("active_client_fund", sa.Column("fa_name", sa.Text(), nullable=True))
    op.add_column("active_client_fund", sa.Column("fa_email", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("active_client_fund", "fa_email")
    op.drop_column("active_client_fund", "fa_name")
