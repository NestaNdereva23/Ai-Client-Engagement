from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6d2f8a4c1e9"
down_revision: str | Sequence[str] | None = "a4c7e1b9d3f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("client_side", sa.Column("previous_side", sa.Text(), nullable=True))
    op.add_column(
        "client_side", sa.Column("side_changed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_client_side_previous_side",
        "client_side",
        "previous_side in ('active', 'inactive')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_client_side_previous_side", "client_side", type_="check")
    op.drop_column("client_side", "side_changed_at")
    op.drop_column("client_side", "previous_side")
