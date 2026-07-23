"""ingestion_status reference_ts anchor

Revision ID: d41c7a9e2b18
Revises: b8172577d6c8
Create Date: 2026-07-23 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d41c7a9e2b18"
down_revision: str | Sequence[str] | None = "b8172577d6c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingestion_status",
        sa.Column(
            "reference_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("ingestion_status", "reference_ts")
