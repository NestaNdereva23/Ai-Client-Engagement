"""weekly report switches on user access

Revision ID: c4e8a2f6d1b9
Revises: a7c3e9f1b2d4
Create Date: 2026-11-17 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a2f6d1b9"
down_revision: str | Sequence[str] | None = "a7c3e9f1b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "user_access"
_COLUMNS = ("receives_weekly_report", "receives_lead_report")


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column(
            _TABLE, sa.Column(column, sa.Boolean(), server_default="false", nullable=False)
        )


def downgrade() -> None:
    for column in _COLUMNS:
        op.drop_column(_TABLE, column)
