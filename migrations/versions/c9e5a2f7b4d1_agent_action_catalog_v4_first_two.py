"""agent_action_catalog v4: two actions on, every other one off

Revision ID: c9e5a2f7b4d1
Revises: b7c4e1d9f2a3
Create Date: 2026-09-09 14:30:00.000000

Two responses go live: the welcome with a small top up, and the warning that
the fee will empty the account. Both are set so a person reads every message
before it goes anywhere. Every other action in the catalogue is stopped, so
the agent can only choose between the two that are ready and the two that
send nothing at all.

Each live action moves to its own angle, written for it, instead of borrowing
one meant for clients who had already left.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.agents.action_catalog import ActionSpec, load_active_actions, save_action_catalog_version

revision: str = "c9e5a2f7b4d1"
down_revision: str | Sequence[str] | None = "b7c4e1d9f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 4
_PREVIOUS = 3
_VALID_FROM = date(2026, 9, 9)

_EVERY_MESSAGE_REVIEWED = "approve_each"

# The only two that send anything, and the angle each one now uses.
_LIVE_ANGLES = {
    "welcome_and_top_up": "welcome_and_top_up",
    "fee_warning": "fee_warning",
}

# Recording a decision to do nothing contacts nobody, so it stays available
# whatever else is stopped: the agent must always be able to say no.
_STILL_ALLOWED = ("do_nothing", "watch_for_now")


def _carried_over(session: Session) -> list[ActionSpec]:
    """Every action in force today, with only the two live ones left running."""
    specs = []
    for row in load_active_actions(session, _VALID_FROM).values():
        live = row.action_code in _LIVE_ANGLES
        specs.append(
            ActionSpec(
                action_code=row.action_code,
                title=row.title,
                who=row.who,
                evidence_required=row.evidence_required,
                response_kind=row.response_kind,
                content_mix=row.content_mix,
                default_permission=(_EVERY_MESSAGE_REVIEWED if live else row.default_permission),
                message_angle=_LIVE_ANGLES.get(row.action_code, row.message_angle),
                channel=row.channel,
                money_ceiling_kes=row.money_ceiling_kes,
                paused=not (live or row.action_code in _STILL_ALLOWED),
            )
        )
    return specs


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    save_action_catalog_version(session, _VERSION, _carried_over(session), valid_from=_VALID_FROM)
    session.flush()


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM agent_action_catalog WHERE version = :version").bindparams(
            version=_VERSION
        )
    )
    op.execute(
        sa.text(
            "UPDATE agent_action_catalog SET valid_to = NULL "
            "WHERE version = :previous AND valid_to = :valid_from"
        ).bindparams(previous=_PREVIOUS, valid_from=_VALID_FROM)
    )
