from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "7c3e750bad4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "client_features",
        sa.Column("never_funded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute("UPDATE client_features SET never_funded = true WHERE priority_tier = 'hot_leads'")


def downgrade() -> None:
    op.drop_column("client_features", "never_funded")
