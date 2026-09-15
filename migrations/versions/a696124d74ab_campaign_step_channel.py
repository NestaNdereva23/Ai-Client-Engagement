"""campaign step channel

Revision ID: a696124d74ab
Revises: e1c7a4f9b3d2
Create Date: 2026-09-13 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a696124d74ab"
down_revision: str | Sequence[str] | None = "e1c7a4f9b3d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("default_channel", sa.Text(), nullable=False, server_default="email"),
    )
    op.add_column("campaign_step", sa.Column("channel", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaign_step", "channel")
    op.drop_column("campaign", "default_channel")
