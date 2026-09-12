from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1c7a4f9b3d2"
down_revision: str | Sequence[str] | None = "d4e8f2a6c1b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rag_settings",
        sa.Column("setting_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("setting_id"),
    )
    op.bulk_insert(
        sa.table(
            "rag_settings",
            sa.column("setting_id", sa.BigInteger),
            sa.column("enabled", sa.Boolean),
        ),
        [{"setting_id": 1, "enabled": True}],
    )


def downgrade() -> None:
    op.drop_table("rag_settings")
