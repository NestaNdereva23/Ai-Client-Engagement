from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7e3c9a1d5f2"
down_revision: str | Sequence[str] | None = "e1b6d3a8c4f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_setting",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("key"),
    )
    op.execute(
        "INSERT INTO app_setting (key, value, updated_by) "
        "SELECT 'rag_enabled', 'false'::jsonb, 'migration' "
        "FROM rag_settings WHERE setting_id = 1 AND enabled = false"
    )
    op.drop_table("rag_settings")


def downgrade() -> None:
    op.create_table(
        "rag_settings",
        sa.Column("setting_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("setting_id"),
    )
    op.execute(
        "INSERT INTO rag_settings (setting_id, enabled) "
        "SELECT 1, NOT EXISTS ("
        "SELECT 1 FROM app_setting WHERE key = 'rag_enabled' AND value = 'false'::jsonb)"
    )
    op.drop_table("app_setting")
