"""campaign step angle optional

Revision ID: c5e8b1d7a4f3
Revises: d3a7c1e9f5b2
Create Date: 2026-08-31 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5e8b1d7a4f3"
down_revision: str | Sequence[str] | None = "d3a7c1e9f5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("campaign_step", "message_angle", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.execute(
        "UPDATE campaign_step SET message_angle = 'pick_up_again' WHERE message_angle IS NULL"
    )
    op.alter_column("campaign_step", "message_angle", existing_type=sa.Text(), nullable=False)
