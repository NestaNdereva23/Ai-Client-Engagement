"""Hold how much freedom each action has, and start every one of them at suggest only.

The action catalogue says what an action is. This table says what it may do
today, so the setting can be tightened or loosened without shipping a new
catalogue version. Every change writes an audit row, so the trail lives in
audit_log rather than in extra rows here.

Revision ID: c4e8a2d6f7b1
Revises: b3d7f1a9c5e2
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.agents.permissions import seed_default_permissions

# revision identifiers, used by Alembic.
revision: str = "c4e8a2d6f7b1"
down_revision: str | Sequence[str] | None = "b3d7f1a9c5e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEEDED_ON = date(2026, 9, 7)
_SEEDED_BY = "system"
_SEEDED_REASON = "starting setting: nothing runs without a person saying yes"

# The columns as they stood when this migration shipped. Written out here,
# instead of importing the live agent_action_catalog model, so a column added
# to that table later never breaks this migration when history is replayed
# from scratch.
_CATALOG_COLUMNS = ("catalog_id", "version", "action_code", "valid_from", "valid_to")
_CATALOG_TABLE = sa.table("agent_action_catalog", *(sa.column(name) for name in _CATALOG_COLUMNS))


def _active_action_codes(bind: sa.engine.Connection, at: date) -> list[str]:
    """Action codes in the catalogue version in force on `at`."""
    version = bind.execute(
        sa.select(_CATALOG_TABLE.c.version)
        .where(
            _CATALOG_TABLE.c.valid_from <= at,
            sa.or_(_CATALOG_TABLE.c.valid_to.is_(None), _CATALOG_TABLE.c.valid_to > at),
        )
        .order_by(_CATALOG_TABLE.c.valid_from.desc(), _CATALOG_TABLE.c.version.desc())
        .limit(1)
    ).scalar()
    if version is None:
        return []
    return list(
        bind.execute(
            sa.select(_CATALOG_TABLE.c.action_code)
            .where(_CATALOG_TABLE.c.version == version)
            .order_by(_CATALOG_TABLE.c.catalog_id)
        ).scalars()
    )


def upgrade() -> None:
    op.create_table(
        "agent_permission",
        sa.Column("permission_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("action_code", sa.Text(), nullable=False),
        sa.Column("priority_tier", sa.Text(), nullable=True),
        sa.Column("risk_band", sa.Text(), nullable=True),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.Column("max_clients_per_day", sa.Integer(), nullable=True),
        sa.Column("max_money_kes", sa.Float(), nullable=True),
        sa.Column("changed_by", sa.Text(), nullable=True),
        sa.Column("changed_reason", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "permission IN ('suggest_only', 'approve_each', 'approve_sample', 'act_alone')",
            name="ck_agent_permission_permission",
        ),
        sa.CheckConstraint(
            "max_clients_per_day IS NULL OR max_clients_per_day > 0",
            name="ck_agent_permission_max_clients_positive",
        ),
        sa.CheckConstraint(
            "max_money_kes IS NULL OR max_money_kes > 0",
            name="ck_agent_permission_max_money_positive",
        ),
        sa.PrimaryKeyConstraint("permission_id"),
        sa.UniqueConstraint(
            "action_code",
            "priority_tier",
            "risk_band",
            name="uq_agent_permission_action_tier_band",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        op.f("ix_agent_permission_action_code"),
        "agent_permission",
        ["action_code"],
        unique=False,
    )

    bind = op.get_bind()
    action_codes = _active_action_codes(bind, _SEEDED_ON)
    session = Session(bind=bind)
    seed_default_permissions(
        session,
        action_codes,
        changed_by=_SEEDED_BY,
        changed_reason=_SEEDED_REASON,
    )
    session.flush()


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_permission_action_code"), table_name="agent_permission")
    op.drop_table("agent_permission")
