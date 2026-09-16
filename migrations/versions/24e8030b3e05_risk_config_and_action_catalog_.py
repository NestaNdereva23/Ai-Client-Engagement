"""risk config and action catalog lifecycle

Adds the same draft/published/archived lifecycle columns tier_contract,
message_angle_catalog, and business_rules already carry to
risk_config_version and agent_action_catalog, so both can be registered in
rules/versioning.py's COMPONENTS registry instead of keeping their own
simpler save and load logic.

valid_from becomes nullable on both tables: a draft row has no validity
window until it is published. Every existing row is backfilled to
status='published', matching what it already behaves as today.

Revision ID: 24e8030b3e05
Revises: 72e4491ed7f5
Create Date: 2026-09-15 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "24e8030b3e05"
down_revision: str | Sequence[str] | None = "72e4491ed7f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIFECYCLE_TABLES = ("risk_config_version", "agent_action_catalog")


def _add_lifecycle_columns(table: str) -> None:
    op.add_column(table, sa.Column("status", sa.Text(), nullable=False, server_default="published"))
    op.add_column(table, sa.Column("created_by", sa.Text(), nullable=True))
    op.add_column(table, sa.Column("published_by", sa.Text(), nullable=True))
    op.add_column(table, sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        f"ck_{table}_status", table, "status IN ('draft', 'published', 'archived')"
    )
    op.alter_column(table, "valid_from", existing_type=sa.Date(), nullable=True)


def _drop_lifecycle_columns(table: str) -> None:
    op.alter_column(table, "valid_from", existing_type=sa.Date(), nullable=False)
    op.drop_constraint(f"ck_{table}_status", table, type_="check")
    op.drop_column(table, "published_at")
    op.drop_column(table, "published_by")
    op.drop_column(table, "created_by")
    op.drop_column(table, "status")


def upgrade() -> None:
    for table in _LIFECYCLE_TABLES:
        _add_lifecycle_columns(table)


def downgrade() -> None:
    for table in reversed(_LIFECYCLE_TABLES):
        _drop_lifecycle_columns(table)
