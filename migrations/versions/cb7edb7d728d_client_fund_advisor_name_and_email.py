"""client_fund advisor name and email

Revision ID: cb7edb7d728d
Revises: b7e3c9a1d5f2
Create Date: 2026-09-30 12:59:31.886821

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cb7edb7d728d"
down_revision: str | Sequence[str] | None = "b7e3c9a1d5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("client_fund", sa.Column("fa_name", sa.Text(), nullable=True))
    op.add_column("client_fund", sa.Column("fa_email", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("client_fund", "fa_email")
    op.drop_column("client_fund", "fa_name")
