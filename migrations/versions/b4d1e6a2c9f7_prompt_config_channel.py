"""prompt config channel

Revision ID: b4d1e6a2c9f7
Revises: f3a8c9e1b7d4
Create Date: 2026-09-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4d1e6a2c9f7"
down_revision: str | Sequence[str] | None = "f3a8c9e1b7d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("voice_contract", "safety_policy", "output_policy")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table, sa.Column("channel", sa.Text(), nullable=False, server_default="email")
        )
        op.drop_constraint(f"uq_{table}_version", table, type_="unique")
        op.create_unique_constraint(f"uq_{table}_channel_version", table, ["channel", "version"])


def downgrade() -> None:
    for table in _TABLES:
        op.drop_constraint(f"uq_{table}_channel_version", table, type_="unique")
        op.create_unique_constraint(f"uq_{table}_version", table, ["version"])
        op.drop_column(table, "channel")
