"""every message reviewed for the first two live actions

Revision ID: d3b8f6c1a9e4
Revises: c9e5a2f7b4d1
Create Date: 2026-09-09 15:00:00.000000

The two actions that go live may now be carried out rather than only
suggested, and the level they are set to means a person reads every single
message before it leaves. Nothing else changes level.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.orm import Session

from app.agents.permissions import set_permission

revision: str = "d3b8f6c1a9e4"
down_revision: str | Sequence[str] | None = "c9e5a2f7b4d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIONS = ("welcome_and_top_up", "fee_warning")
_EVERY_MESSAGE_REVIEWED = "approve_each"
_BACK_TO = "suggest_only"
_CHANGED_BY = "delivery"
_REASON = "the first two actions go live, with a person reading every message"


def _set(level: str, reason: str) -> None:
    session = Session(bind=op.get_bind())
    for action_code in _ACTIONS:
        set_permission(
            session,
            action_code,
            level,
            changed_by=_CHANGED_BY,
            changed_reason=reason,
        )
    session.flush()


def upgrade() -> None:
    _set(_EVERY_MESSAGE_REVIEWED, _REASON)


def downgrade() -> None:
    _set(_BACK_TO, "the first two actions go back to being suggestions only")
