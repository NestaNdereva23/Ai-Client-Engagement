"""generation run reproducibility stamp

Revision ID: a3681932b889
Revises: b02e425b3303
Create Date: 2026-08-06 09:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3681932b889"
down_revision: str | Sequence[str] | None = "b02e425b3303"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("generation_runs", sa.Column("data_date", sa.Date(), nullable=True))
    op.add_column("generation_runs", sa.Column("rule_version", sa.Integer(), nullable=True))
    op.add_column(
        "generation_runs", sa.Column("angle_catalog_version", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("generation_runs", "angle_catalog_version")
    op.drop_column("generation_runs", "rule_version")
    op.drop_column("generation_runs", "data_date")
