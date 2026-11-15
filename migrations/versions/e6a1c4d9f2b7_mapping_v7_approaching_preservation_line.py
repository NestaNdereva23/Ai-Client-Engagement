"""situation_action_mapping 7: clients heading below the active line get an advisor task"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.agents.situation_action_mapping import MappingSpec, save_situation_action_mapping_version

revision: str = "e6a1c4d9f2b7"
down_revision: str | Sequence[str] | None = "a7d3f9c2e6b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 7
_PREVIOUS = 6
_VALID_FROM = date(2026, 10, 14)

_NEW_MAPPING = MappingSpec(
    situation="approaching_preservation_line",
    action_code="give_advisor_a_task",
    objective="protect",
    angle="sitting_still",
    evidence_required="A balance on course to fall below the active line within the horizon",
    channel="email",
)


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    carried = session.execute(
        sa.text(
            "SELECT situation, action_code, objective, angle, evidence_required, channel, priority "
            "FROM situation_action_mapping WHERE version = :previous ORDER BY mapping_id"
        ).bindparams(previous=_PREVIOUS)
    ).all()
    mappings = [MappingSpec(*row) for row in carried]
    mappings.append(_NEW_MAPPING)
    save_situation_action_mapping_version(session, _VERSION, mappings, valid_from=_VALID_FROM)
    session.flush()


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM situation_action_mapping WHERE version = :version").bindparams(
            version=_VERSION
        )
    )
    op.execute(
        sa.text(
            "UPDATE situation_action_mapping SET valid_to = NULL "
            "WHERE version = :previous AND valid_to = :valid_from"
        ).bindparams(previous=_PREVIOUS, valid_from=_VALID_FROM)
    )
    op.execute(
        sa.text(
            "UPDATE active_configuration SET active_version = :previous "
            "WHERE component_type = 'situation_action_mapping' AND active_version = :version"
        ).bindparams(previous=_PREVIOUS, version=_VERSION)
    )
