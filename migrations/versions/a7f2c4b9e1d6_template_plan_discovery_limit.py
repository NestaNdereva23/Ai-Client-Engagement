"""template plan discovery limit

Revision ID: a7f2c4b9e1d6
Revises: c5e8b1d7a4f3
Create Date: 2026-08-31 10:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7f2c4b9e1d6"
down_revision: str | Sequence[str] | None = "c5e8b1d7a4f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "template_generation_plan",
        sa.Column("discovery_limit", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_tgp_discovery_limit_positive",
        "template_generation_plan",
        "discovery_limit IS NULL OR discovery_limit > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tgp_discovery_limit_positive", "template_generation_plan", type_="check")
    op.drop_column("template_generation_plan", "discovery_limit")
