"""suppression table

Revision ID: 93a7ef0289bd
Revises: 6fe7a06e69a0
Create Date: 2026-07-30 20:38:37.395474

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "93a7ef0289bd"
down_revision: str | Sequence[str] | None = "6fe7a06e69a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# client_id is a restricted re-attachment key (CLAUDE.md §7), so suppression
# gets the same treatment as pii_vault: restricted role only, never safe.
RESTRICTED = "ace_restricted"
SAFE = "ace_safe"


def upgrade() -> None:
    op.create_table(
        "suppression",
        sa.Column("client_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("client_id"),
    )

    op.execute("REVOKE ALL ON suppression FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON suppression TO {RESTRICTED}")
    op.execute(f"REVOKE ALL ON suppression FROM {SAFE}")


def downgrade() -> None:
    op.drop_table("suppression")
