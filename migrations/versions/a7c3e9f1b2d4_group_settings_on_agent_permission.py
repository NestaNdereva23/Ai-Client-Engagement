"""group settings on agent permission

Revision ID: a7c3e9f1b2d4
Revises: 62ea4e34fed7
Create Date: 2026-11-10 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a7c3e9f1b2d4"
down_revision: str | Sequence[str] | None = "62ea4e34fed7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_permission"
_LIVE_INDEX = "uq_agent_permission_action_tier_band_live"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("group_name", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("fa_id", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("balance_filter", sa.Boolean(), nullable=True))
    op.add_column(_TABLE, sa.Column("min_balance", sa.Float(), nullable=True))
    op.add_column(_TABLE, sa.Column("max_balance", sa.Float(), nullable=True))
    op.add_column(_TABLE, sa.Column("content_mix", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("guide_doc_ids", JSONB(), nullable=True))
    op.add_column(_TABLE, sa.Column("use_rates", sa.Boolean(), nullable=True))
    op.add_column(_TABLE, sa.Column("no_repeat", sa.Boolean(), nullable=True))
    op.add_column(_TABLE, sa.Column("paused", sa.Boolean(), nullable=True))
    op.alter_column(_TABLE, "action_code", nullable=True)
    op.alter_column(_TABLE, "permission", nullable=True)
    op.create_index("ix_agent_permission_group_name", _TABLE, ["group_name"])

    op.drop_constraint("ck_agent_permission_max_clients_positive", _TABLE, type_="check")
    op.create_check_constraint(
        "ck_agent_permission_max_clients_positive",
        _TABLE,
        "max_clients_per_day IS NULL OR max_clients_per_day >= 0",
    )
    op.create_check_constraint(
        "ck_agent_permission_action_or_group",
        _TABLE,
        "(action_code IS NULL) <> (group_name IS NULL)",
    )
    op.create_check_constraint(
        "ck_agent_permission_action_has_level",
        _TABLE,
        "action_code IS NULL OR permission IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_agent_permission_content_mix",
        _TABLE,
        "content_mix IS NULL OR content_mix IN "
        "('learning_only', 'mostly_learning', 'balanced', 'mostly_ask')",
    )
    op.create_check_constraint(
        "ck_agent_permission_balance_range",
        _TABLE,
        "(min_balance IS NULL OR min_balance >= 0) AND "
        "(max_balance IS NULL OR min_balance IS NULL OR max_balance > min_balance)",
    )

    op.drop_index(_LIVE_INDEX, table_name=_TABLE)
    op.create_index(
        _LIVE_INDEX,
        _TABLE,
        ["action_code", "priority_tier", "risk_band", "group_name", "fa_id"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
    op.add_column("agent_proposal", sa.Column("guide_version_ids", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_proposal", "guide_version_ids")
    op.execute(sa.text("DELETE FROM agent_permission WHERE group_name IS NOT NULL"))

    op.drop_index(_LIVE_INDEX, table_name=_TABLE)
    op.create_index(
        _LIVE_INDEX,
        _TABLE,
        ["action_code", "priority_tier", "risk_band"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
    for name in (
        "ck_agent_permission_balance_range",
        "ck_agent_permission_content_mix",
        "ck_agent_permission_action_has_level",
        "ck_agent_permission_action_or_group",
        "ck_agent_permission_max_clients_positive",
    ):
        op.drop_constraint(name, _TABLE, type_="check")
    op.execute(
        sa.text(
            "UPDATE agent_permission SET max_clients_per_day = NULL WHERE max_clients_per_day = 0"
        )
    )
    op.create_check_constraint(
        "ck_agent_permission_max_clients_positive",
        _TABLE,
        "max_clients_per_day IS NULL OR max_clients_per_day > 0",
    )
    op.drop_index("ix_agent_permission_group_name", table_name=_TABLE)
    op.alter_column(_TABLE, "permission", nullable=False)
    op.alter_column(_TABLE, "action_code", nullable=False)
    for column in (
        "paused",
        "no_repeat",
        "use_rates",
        "guide_doc_ids",
        "content_mix",
        "max_balance",
        "min_balance",
        "balance_filter",
        "fa_id",
        "group_name",
    ):
        op.drop_column(_TABLE, column)
