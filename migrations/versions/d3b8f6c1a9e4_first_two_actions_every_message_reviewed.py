"""every message reviewed for the first two live actions

Revision ID: d3b8f6c1a9e4
Revises: c9e5a2f7b4d1
Create Date: 2026-09-09 15:00:00.000000

The two actions that go live may now be carried out rather than only
suggested, and the level they are set to means a person reads every single
message before it leaves. Nothing else changes level.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3b8f6c1a9e4"
down_revision: str | Sequence[str] | None = "c9e5a2f7b4d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIONS = ("welcome_and_top_up", "fee_warning")
_EVERY_MESSAGE_REVIEWED = "approve_each"
_BACK_TO = "suggest_only"
_CHANGED_BY = "delivery"
_REASON = "the first two actions go live, with a person reading every message"

# The columns as they stood when this migration shipped, before agent_permission
# was versioned with valid_from/valid_to.
_PERMISSION_COLUMNS = (
    "action_code",
    "priority_tier",
    "risk_band",
    "permission",
    "changed_by",
    "changed_reason",
)
_PERMISSION_TABLE = sa.table("agent_permission", *(sa.column(name) for name in _PERMISSION_COLUMNS))


def _set(level: str, reason: str) -> None:
    bind = op.get_bind()
    for action_code in _ACTIONS:
        result = bind.execute(
            _PERMISSION_TABLE.update()
            .where(
                _PERMISSION_TABLE.c.action_code == action_code,
                _PERMISSION_TABLE.c.priority_tier.is_(None),
                _PERMISSION_TABLE.c.risk_band.is_(None),
            )
            .values(permission=level, changed_by=_CHANGED_BY, changed_reason=reason)
        )
        if result.rowcount == 0:
            bind.execute(
                _PERMISSION_TABLE.insert().values(
                    action_code=action_code,
                    priority_tier=None,
                    risk_band=None,
                    permission=level,
                    changed_by=_CHANGED_BY,
                    changed_reason=reason,
                )
            )


def upgrade() -> None:
    _set(_EVERY_MESSAGE_REVIEWED, _REASON)


def downgrade() -> None:
    _set(_BACK_TO, "the first two actions go back to being suggestions only")
