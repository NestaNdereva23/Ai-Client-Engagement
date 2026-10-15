"""agent_action_catalog: switch give_advisor_a_task on so advisor task suggestions show"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.agents.action_catalog import set_action_paused

revision: str = "f2b8d4a6c1e9"
down_revision: str | Sequence[str] | None = "e6a1c4d9f2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTION = "give_advisor_a_task"
_VALID_FROM = date(2026, 10, 14)


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    set_action_paused(session, _ACTION, False, changed_by="active_tiers", valid_from=_VALID_FROM)
    session.flush()


def downgrade() -> None:
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT on_row.version, off_row.version FROM agent_action_catalog on_row "
                "JOIN agent_action_catalog off_row ON off_row.action_code = on_row.action_code "
                "AND off_row.valid_to = on_row.valid_from "
                "WHERE on_row.action_code = :code AND on_row.paused = false "
                "AND off_row.paused = true AND on_row.valid_from = :valid_from "
                "ORDER BY on_row.version LIMIT 1"
            ).bindparams(code=_ACTION, valid_from=_VALID_FROM)
        )
        .first()
    )
    if row is None:
        return
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
