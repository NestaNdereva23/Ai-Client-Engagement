"""idempotency keys table

Revision ID: 6fe7a06e69a0
Revises: 1b334ff8ae77
Create Date: 2026-07-30 15:34:36.275991

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "6fe7a06e69a0"
down_revision: str | Sequence[str] | None = "1b334ff8ae77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("idempotency_key", sa.Text(), autoincrement=False, nullable=False),
        sa.Column("method", sa.Text(), autoincrement=False, nullable=False),
        sa.Column("path", sa.Text(), autoincrement=False, nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("idempotency_key", "method", "path"),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
