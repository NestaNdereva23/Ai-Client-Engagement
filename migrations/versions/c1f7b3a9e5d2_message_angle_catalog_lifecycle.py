"""message_angle_catalog lifecycle columns

Split out of the prompt config versioning foundation migration so it runs
right after message_angle_catalog is created, not near the end of history.
The message_angle_catalog v2 seed, much later in this chain, calls
save_catalog_version(), which has always written status; replaying that seed
from an empty database failed because the column didn't exist yet at that
point in the chain.

Revision ID: c1f7b3a9e5d2
Revises: e6a3c8d5f2b1
Create Date: 2026-09-12 00:00:01.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1f7b3a9e5d2"
down_revision: str | Sequence[str] | None = "e6a3c8d5f2b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "message_angle_catalog",
        sa.Column("status", sa.Text(), nullable=False, server_default="published"),
    )
    op.add_column("message_angle_catalog", sa.Column("created_by", sa.Text(), nullable=True))
    op.add_column("message_angle_catalog", sa.Column("published_by", sa.Text(), nullable=True))
    op.add_column(
        "message_angle_catalog",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_message_angle_catalog_status",
        "message_angle_catalog",
        "status IN ('draft', 'published', 'archived')",
    )
    op.alter_column("message_angle_catalog", "valid_from", existing_type=sa.Date(), nullable=True)


def downgrade() -> None:
    op.alter_column("message_angle_catalog", "valid_from", existing_type=sa.Date(), nullable=False)
    op.drop_constraint("ck_message_angle_catalog_status", "message_angle_catalog", type_="check")
    op.drop_column("message_angle_catalog", "published_at")
    op.drop_column("message_angle_catalog", "published_by")
    op.drop_column("message_angle_catalog", "created_by")
    op.drop_column("message_angle_catalog", "status")
