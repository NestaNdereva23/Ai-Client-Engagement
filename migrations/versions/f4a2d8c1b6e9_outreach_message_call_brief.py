"""outreach_message.call_brief column

Revision ID: f4a2d8c1b6e9
Revises: e3b7c1f9a4d6
Create Date: 2026-08-09 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4a2d8c1b6e9"
down_revision: str | Sequence[str] | None = "e3b7c1f9a4d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("outreach_message", sa.Column("call_brief", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("outreach_message", "call_brief")
