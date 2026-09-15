"""touch log provider result

Revision ID: 48ed61112a9a
Revises: b4d1e6a2c9f7
Create Date: 2026-09-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "48ed61112a9a"
down_revision: str | Sequence[str] | None = "b4d1e6a2c9f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("touch_log", sa.Column("provider_status", sa.Text(), nullable=True))
    op.add_column("touch_log", sa.Column("parts", sa.Integer(), nullable=True))
    op.add_column("touch_log", sa.Column("cost", sa.Numeric(10, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("touch_log", "cost")
    op.drop_column("touch_log", "parts")
    op.drop_column("touch_log", "provider_status")
