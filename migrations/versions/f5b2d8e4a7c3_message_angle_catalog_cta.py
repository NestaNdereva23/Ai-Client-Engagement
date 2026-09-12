from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5b2d8e4a7c3"
down_revision: str | Sequence[str] | None = "c1f7b3a9e5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("message_angle_catalog", sa.Column("cta", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("message_angle_catalog", "cta")
