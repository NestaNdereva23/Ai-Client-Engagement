"""client_complaint table

Revision ID: 8aaa8e4abb19
Revises: bae2554b5bac
Create Date: 2026-08-10 22:14:40.245801

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8aaa8e4abb19"
down_revision: str | Sequence[str] | None = "bae2554b5bac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_complaint",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("opened_at", sa.Date(), nullable=False),
        sa.Column("closed_at", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), server_default="stub", nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "category IN ('billing', 'service', 'product', 'other')",
            name="ck_client_complaint_category",
        ),
        sa.CheckConstraint(
            "channel IN ('call', 'email', 'branch', 'other')", name="ck_client_complaint_channel"
        ),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_client_complaint_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_client_complaint_client_id", "client_complaint", ["client_id"])
    # No grant to the model-facing role; holds no free-text, but is not
    # something a draft prompt reads from directly.


def downgrade() -> None:
    op.drop_index("ix_client_complaint_client_id", table_name="client_complaint")
    op.drop_table("client_complaint")
