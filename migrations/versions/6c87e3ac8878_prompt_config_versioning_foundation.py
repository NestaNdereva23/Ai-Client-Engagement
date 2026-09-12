"""prompt config versioning foundation

Adds a draft/published/archived lifecycle to tier_contract (the other two
versioned config tables, message_angle_catalog and business_rules, got this
same lifecycle earlier in this chain, right after each one was created --
their own seed migrations call it, and needed it that much sooner). Also
creates voice_contract, safety_policy, output_policy, and
personalization_policy as empty shells, so the shared versioning module has
real tables to run against before their own content columns land.

valid_from becomes nullable on tier_contract: a draft row has no validity
window until it is published.

Every existing row is backfilled to status='published', matching what it
already behaves as today.

Revision ID: 6c87e3ac8878
Revises: f7d2a9c4e1b3
Create Date: 2026-09-11 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6c87e3ac8878"
down_revision: str | Sequence[str] | None = "f7d2a9c4e1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIFECYCLE_TABLES = ("tier_contract",)
_POLICY_SHELLS = ("voice_contract", "safety_policy", "output_policy", "personalization_policy")


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


def _create_policy_shell(table: str) -> None:
    op.create_table(
        table,
        sa.Column(f"{table}_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="published"),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("published_by", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(f"{table}_id"),
        sa.UniqueConstraint("version", name=f"uq_{table}_version"),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')", name=f"ck_{table}_status"
        ),
    )
    op.create_index(f"ix_{table}_version", table, ["version"])


def upgrade() -> None:
    for table in _LIFECYCLE_TABLES:
        _add_lifecycle_columns(table)

    for table in _POLICY_SHELLS:
        _create_policy_shell(table)


def downgrade() -> None:
    for table in reversed(_POLICY_SHELLS):
        op.drop_index(f"ix_{table}_version", table_name=table)
        op.drop_table(table)

    for table in reversed(_LIFECYCLE_TABLES):
        _drop_lifecycle_columns(table)
