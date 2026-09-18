"""test recipient table

Revision ID: a4c8e2f61b93
Revises: e3a9f6c2b7d4
Create Date: 2026-09-18 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4c8e2f61b93"
down_revision: str | Sequence[str] | None = "e3a9f6c2b7d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Team contacts are personal data too, so only the restricted role can read them.
RESTRICTED = "ace_restricted"
SAFE = "ace_safe"


def upgrade() -> None:
    op.create_table(
        "test_recipient",
        sa.Column("recipient_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "email IS NOT NULL OR phone IS NOT NULL", name="ck_test_recipient_has_contact"
        ),
        sa.PrimaryKeyConstraint("recipient_id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("phone"),
    )

    op.execute("REVOKE ALL ON test_recipient FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON test_recipient TO {RESTRICTED}")
    op.execute(f"GRANT USAGE ON SEQUENCE test_recipient_recipient_id_seq TO {RESTRICTED}")
    op.execute(f"REVOKE ALL ON test_recipient FROM {SAFE}")


def downgrade() -> None:
    op.drop_table("test_recipient")
