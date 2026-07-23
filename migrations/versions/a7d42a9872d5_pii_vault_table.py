"""pii_vault table

Revision ID: a7d42a9872d5
Revises: 29c0903cd101
Create Date: 2026-07-23 10:24:48.850175

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d42a9872d5"
down_revision: str | Sequence[str] | None = "29c0903cd101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pii_vault",
        sa.Column("client_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("client_name", sa.Text(), nullable=True),
        sa.Column("contact_email", sa.Text(), nullable=True),
        sa.Column("contact_whatsapp", sa.Text(), nullable=True),
        sa.Column("opt_out_flag", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("client_id"),
    )


def downgrade() -> None:
    op.drop_table("pii_vault")
