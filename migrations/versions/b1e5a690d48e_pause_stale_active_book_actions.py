"""agent_action_catalog: pause start_win_back and fee_pressure_warning_dormant"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.agents.action_catalog import set_action_paused

revision: str = "b1e5a690d48e"
down_revision: str | Sequence[str] | None = "f3a8c261d9b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIONS = ("start_win_back", "fee_pressure_warning_dormant")
_VALID_FROM = date(2026, 10, 21)


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    for action in _ACTIONS:
        set_action_paused(
            session, action, True, changed_by="angle_generation", valid_from=_VALID_FROM
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
                "WHERE on_row.action_code = :code AND on_row.paused = true "
                "AND off_row.paused = false AND on_row.valid_from = :valid_from "
                "ORDER BY on_row.version LIMIT 1"
            ).bindparams(code=action, valid_from=_VALID_FROM)
        ).first()
        if row is None:
            continue
        paused_version, unpaused_version = row
        op.execute(
            sa.text("DELETE FROM agent_action_catalog WHERE version = :version").bindparams(
                version=paused_version
            )
        )
        op.execute(
            sa.text(
                "UPDATE agent_action_catalog SET valid_to = NULL "
                "WHERE version = :previous AND valid_to = :valid_from"
            ).bindparams(previous=unpaused_version, valid_from=_VALID_FROM)
        )
        op.execute(
            sa.text(
                "UPDATE active_configuration SET active_version = :previous "
                "WHERE component_type = 'agent_action_catalog' AND active_version = :version"
            ).bindparams(previous=unpaused_version, version=paused_version)
        )
