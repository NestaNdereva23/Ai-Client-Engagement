"""angle tone and mapping priority

Adds tone to message_angle_catalog and priority to situation_action_mapping,
both nullable so every existing published row keeps loading unchanged.

Revision ID: c26d540455aa
Revises: b6d4e8a1f302
Create Date: 2026-09-16 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c26d540455aa"
down_revision: str | Sequence[str] | None = "b6d4e8a1f302"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("message_angle_catalog", sa.Column("tone", sa.Text(), nullable=True))
    op.add_column("situation_action_mapping", sa.Column("priority", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("situation_action_mapping", "priority")
    op.drop_column("message_angle_catalog", "tone")
