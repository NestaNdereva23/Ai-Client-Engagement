"""agent_action_catalog: unpause start_win_back and fee_pressure_warning_dormant

Both actions are ready: the briefs and situation mappings were written in the
remaining_actions_on migration and the review gate is 'suggest_only', so every
email still waits for a person before it goes. Re-pausing them after that was
overly cautious; this reverses that pause.

Revision ID: c1e4a9f7b2d3
Revises: b1e5a690d48e
Create Date: 2026-10-21 00:00:00.000000
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.agents.action_catalog import set_action_paused

revision: str = "c1e4a9f7b2d3"
down_revision: str | Sequence[str] | None = "b1e5a690d48e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIONS = ("start_win_back", "fee_pressure_warning_dormant")
_VALID_FROM = date(2026, 10, 21)


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    for action in _ACTIONS:
        set_action_paused(
            session, action, False, changed_by="ready_for_launch", valid_from=_VALID_FROM
        )
    session.flush()


def downgrade() -> None:
    bind = op.get_bind()
    for action in reversed(_ACTIONS):
        row = bind.execute(
            sa.text(
                "SELECT on_row.version, off_row.version FROM agent_action_catalog on_row "
                "JOIN agent_action_catalog off_row ON off_row.action_code = on_row.action_code "
                "AND off_row.valid_to = on_row.valid_from "
                "WHERE on_row.action_code = :code AND on_row.paused = false "
                "AND off_row.paused = true AND on_row.valid_from = :valid_from "
                "ORDER BY on_row.version LIMIT 1"
            ).bindparams(code=action, valid_from=_VALID_FROM)
        ).first()
        if row is None:
            continue
        unpaused_version, paused_version = row
        op.execute(
            sa.text("DELETE FROM agent_action_catalog WHERE version = :version").bindparams(
                version=unpaused_version
            )
        )
        op.execute(
            sa.text(
                "UPDATE agent_action_catalog SET valid_to = NULL "
                "WHERE version = :previous AND valid_to = :valid_from"
            ).bindparams(previous=paused_version, valid_from=_VALID_FROM)
        )
        op.execute(
            sa.text(
                "UPDATE active_configuration SET active_version = :previous "
                "WHERE component_type = 'agent_action_catalog' AND active_version = :version"
            ).bindparams(previous=paused_version, version=unpaused_version)
        )
