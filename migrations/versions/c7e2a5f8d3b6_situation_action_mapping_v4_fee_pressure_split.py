"""situation_action_mapping v4: fee pressure splits into two actions

Revision ID: c7e2a5f8d3b6
Revises: b9d3e6f1a2c4
Create Date: 2026-09-16 13:10:00.000000

Carries every mapping in force today forward, drops system_fee_pressure
(its group is retired, replaced by the two below), and adds a row for each
of the two situations that now cover the same population: one gone quiet,
one still contributing.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.agents.situation_action_mapping import (
    MappingSpec,
    load_active_mappings,
    save_situation_action_mapping_version,
)

revision: str = "c7e2a5f8d3b6"
down_revision: str | Sequence[str] | None = "b9d3e6f1a2c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 4
_PREVIOUS = 3
_VALID_FROM = date(2026, 9, 16)
_RETIRED_SITUATION = "system_fee_pressure"

_NEW_MAPPINGS = [
    MappingSpec(
        situation="fee_pressure_gone_quiet",
        action_code="fee_pressure_warning_dormant",
        objective="protect",
        angle="fee_pressure_warning_dormant",
        evidence_required="Months until empty below the threshold, and the dormant signal",
        channel="email",
    ),
    MappingSpec(
        situation="fee_pressure_active_contributor",
        action_code="fee_pressure_encourage_active",
        objective="encourage",
        angle="fee_pressure_encourage_active",
        evidence_required="Months until empty below the threshold, with deposits continuing",
        channel="email",
    ),
]


def _carried_over(session: Session) -> list[MappingSpec]:
    """Every mapping in force today, except the one for the retired group."""
    return [
        MappingSpec(
            situation=row.situation,
            action_code=row.action_code,
            objective=row.objective,
            angle=row.angle,
            evidence_required=row.evidence_required,
            channel=row.channel,
            priority=row.priority,
        )
        for row in load_active_mappings(session, _VALID_FROM)
        if row.situation != _RETIRED_SITUATION
    ]


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    save_situation_action_mapping_version(
        session, _VERSION, [*_carried_over(session), *_NEW_MAPPINGS], valid_from=_VALID_FROM
    )
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
