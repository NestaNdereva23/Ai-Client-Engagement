"""clients purchases_censored flag

Revision ID: ad044d684702
Revises: a7d42a9872d5
Create Date: 2026-07-23 10:46:57.715594

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ad044d684702"
down_revision: str | Sequence[str] | None = "a7d42a9872d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column(
            "purchases_censored", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("clients", "purchases_censored")
