"""agent_action_catalog v6: fee pressure splits into two actions

Revision ID: a4f7c2e9b8d5
Revises: e4a2f6c8d1b3
Create Date: 2026-09-16 13:00:00.000000

The single fee-warning action is joined by a second one: a client under fee
pressure who has gone quiet gets warned, and one who is still paying in gets
encouraged instead, rather than both reading the same message. Every other
action carries over unchanged. Version 5 already exists as an unpublished
draft with no valid_from (separate, unrelated work in progress), so this
skips straight to 6 rather than colliding with it.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.agents.action_catalog import ActionSpec, load_active_actions, save_action_catalog_version

revision: str = "a4f7c2e9b8d5"
down_revision: str | Sequence[str] | None = "e4a2f6c8d1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 6
_PREVIOUS = 4
_VALID_FROM = date(2026, 9, 16)

_NEW_ACTIONS = [
    ActionSpec(
        action_code="fee_pressure_warning_dormant",
        title="Warn a dormant client the fee will empty their account",
        who="Someone under fee pressure who has also gone quiet",
        evidence_required="Months until empty below the threshold, and the dormant signal",
        response_kind="automated_email",
        content_mix="balanced",
        default_permission="approve_each",
        message_angle="fee_pressure_warning_dormant",
        channel="email",
        money_ceiling_kes=500000.0,
    ),
    ActionSpec(
        action_code="fee_pressure_encourage_active",
        title="Encourage a still-contributing client under fee pressure",
        who="Someone under fee pressure who is still paying in",
        evidence_required="Months until empty below the threshold, with deposits continuing",
        response_kind="automated_email",
        content_mix="mostly_learning",
        default_permission="approve_each",
        message_angle="fee_pressure_encourage_active",
        channel="email",
        money_ceiling_kes=500000.0,
    ),
]


def _carried_over(session: Session) -> list[ActionSpec]:
    """Every action in force today, exactly as it reads now."""
    return [
        ActionSpec(
            action_code=row.action_code,
            title=row.title,
            who=row.who,
            evidence_required=row.evidence_required,
            response_kind=row.response_kind,
            content_mix=row.content_mix,
            default_permission=row.default_permission,
            message_angle=row.message_angle,
            channel=row.channel,
            money_ceiling_kes=row.money_ceiling_kes,
            paused=row.paused,
        )
        for row in load_active_actions(session, _VALID_FROM).values()
    ]


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    save_action_catalog_version(
        session, _VERSION, [*_carried_over(session), *_NEW_ACTIONS], valid_from=_VALID_FROM
    )
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
