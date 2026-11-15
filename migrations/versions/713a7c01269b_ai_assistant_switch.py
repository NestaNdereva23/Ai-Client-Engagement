"""user_access: a per person switch for the AI Assistant tab"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "713a7c01269b"
down_revision: str | Sequence[str] | None = "a8d3f5c7e2b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_access",
        sa.Column("ai_assistant", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("user_access", "ai_assistant")
