"""user_access: who may see client data, and the viewed-for column on advisor writes"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c3e7f1b5d2"
down_revision: str | Sequence[str] | None = "f2b8d4a6c1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "user_access"
ON_BEHALF_TABLES = ("active_client_interaction", "active_client_touchpoint")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), server_default="advisor", nullable=False),
        sa.Column("fa_id", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("receives_digest", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("view_as_email", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), server_default="manual", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("role IN ('advisor', 'admin')", name="ck_user_access_role"),
        sa.CheckConstraint("source IN ('auto', 'manual')", name="ck_user_access_source"),
        sa.PrimaryKeyConstraint("email"),
    )
    for table in ON_BEHALF_TABLES:
        op.add_column(table, sa.Column("on_behalf_of", sa.Text(), nullable=True))
    op.execute(
        "INSERT INTO user_access (email, role, source) "
        "SELECT DISTINCT lower(btrim(f.fa_email)), 'advisor', 'auto' "
        "FROM active_client_fund f "
        "JOIN client_side s ON s.client_id = f.client_id AND s.side = 'active' "
        "WHERE f.fa_email IS NOT NULL AND btrim(f.fa_email) <> '' "
        "ON CONFLICT (email) DO NOTHING"
    )


def downgrade() -> None:
    for table in ON_BEHALF_TABLES:
        op.drop_column(table, "on_behalf_of")
    op.drop_table(TABLE)
