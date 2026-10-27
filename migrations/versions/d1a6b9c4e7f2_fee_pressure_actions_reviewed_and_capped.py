"""the two fee pressure actions go live: every message reviewed, and capped

Revision ID: d1a6b9c4e7f2
Revises: c7e2a5f8d3b6
Create Date: 2026-09-16 13:15:00.000000

Both new actions go to approve_each, the same level as every other live
action, so a person reads every message before it leaves. Unlike the
existing live actions, both also get a daily client cap: the situation each
one targets can match several thousand client funds in one run, and there is
no overall daily cap yet to catch that on its own, so a per-action cap
stands in until one exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1a6b9c4e7f2"
down_revision: str | Sequence[str] | None = "c7e2a5f8d3b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIONS = ("fee_pressure_warning_dormant", "fee_pressure_encourage_active")
_EVERY_MESSAGE_REVIEWED = "approve_each"
_BACK_TO = "suggest_only"
_CHANGED_BY = "delivery"
_DAILY_CAP = 100

# The columns as they stood when this migration shipped, before agent_permission
# was versioned with valid_from/valid_to.
_PERMISSION_COLUMNS = (
    "action_code",
    "priority_tier",
    "risk_band",
    "permission",
    "max_clients_per_day",
    "changed_by",
    "changed_reason",
)
_PERMISSION_TABLE = sa.table("agent_permission", *(sa.column(name) for name in _PERMISSION_COLUMNS))


def _set(level: str, reason: str, *, max_clients_per_day: int | None) -> None:
    bind = op.get_bind()
    for action_code in _ACTIONS:
        result = bind.execute(
            _PERMISSION_TABLE.update()
            .where(
                _PERMISSION_TABLE.c.action_code == action_code,
                _PERMISSION_TABLE.c.priority_tier.is_(None),
                _PERMISSION_TABLE.c.risk_band.is_(None),
            )
            .values(
                permission=level,
                max_clients_per_day=max_clients_per_day,
                changed_by=_CHANGED_BY,
                changed_reason=reason,
            )
        )
        if result.rowcount == 0:
            bind.execute(
                _PERMISSION_TABLE.insert().values(
                    action_code=action_code,
                    priority_tier=None,
                    risk_band=None,
                    permission=level,
                    max_clients_per_day=max_clients_per_day,
                    changed_by=_CHANGED_BY,
                    changed_reason=reason,
                )
            )


def upgrade() -> None:
    _set(
        _EVERY_MESSAGE_REVIEWED,
        "the two fee pressure actions go live, with a person reading every "
        "message and a daily cap while no overall cap exists yet",
        max_clients_per_day=_DAILY_CAP,
    )


def downgrade() -> None:
    _set(
        _BACK_TO,
        "the two fee pressure actions go back to being suggestions only",
        max_clients_per_day=None,
    )
