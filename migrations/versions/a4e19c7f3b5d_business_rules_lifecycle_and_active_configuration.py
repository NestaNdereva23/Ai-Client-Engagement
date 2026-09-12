"""business_rules lifecycle columns and active_configuration table

Split out of the prompt config versioning foundation migration so it runs
right after business_rules is created, not near the end of history. The
foundation migration originally added these columns much later, but
business_rules v3's seed already calls save_version(), which has always
written status and used active_configuration -- replaying that seed from an
empty database failed because those didn't exist yet at that point in the
chain. active_configuration is created here, once, since every lifecycle
table needs it.

Revision ID: a4e19c7f3b5d
Revises: e1a4c8b6d3f5
Create Date: 2026-09-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4e19c7f3b5d"
down_revision: str | Sequence[str] | None = "e1a4c8b6d3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "active_configuration",
        sa.Column("config_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("component_type", sa.Text(), nullable=False),
        sa.Column("component_key", sa.Text(), nullable=False),
        sa.Column("active_version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("config_id"),
        sa.UniqueConstraint(
            "component_type", "component_key", name="uq_active_configuration_component"
        ),
    )
    op.create_index(
        "ix_active_configuration_component_type", "active_configuration", ["component_type"]
    )

    op.add_column(
        "business_rules",
        sa.Column("status", sa.Text(), nullable=False, server_default="published"),
    )
    op.add_column("business_rules", sa.Column("created_by", sa.Text(), nullable=True))
    op.add_column("business_rules", sa.Column("published_by", sa.Text(), nullable=True))
    op.add_column(
        "business_rules", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_business_rules_status", "business_rules", "status IN ('draft', 'published', 'archived')"
    )
    op.alter_column("business_rules", "valid_from", existing_type=sa.Date(), nullable=True)


def downgrade() -> None:
    op.alter_column("business_rules", "valid_from", existing_type=sa.Date(), nullable=False)
    op.drop_constraint("ck_business_rules_status", "business_rules", type_="check")
    op.drop_column("business_rules", "published_at")
    op.drop_column("business_rules", "published_by")
    op.drop_column("business_rules", "created_by")
    op.drop_column("business_rules", "status")

    op.drop_index("ix_active_configuration_component_type", table_name="active_configuration")
    op.drop_table("active_configuration")
