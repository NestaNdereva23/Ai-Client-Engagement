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

from app.agents.action_catalog import ActionSpec

revision: str = "c9e5a2f7b4d1"
down_revision: str | Sequence[str] | None = "b7c4e1d9f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# status, created_by, published_by and published_at are not yet columns on
# agent_action_catalog at this point in the migration chain (they arrive
# with the lifecycle migration much later), so both the read and the write
# below go against a table snapshot frozen to what exists here rather than
# the live ORM model, which always reflects every column added since.
# Reading or inserting through the live model would ask for columns that do
# not exist yet on a database built from scratch. With no status column yet,
# "currently active" here just means the row is still open.
agent_action_catalog = sa.table(
    "agent_action_catalog",
    sa.column("version", sa.Integer),
    sa.column("action_code", sa.Text),
    sa.column("title", sa.Text),
    sa.column("who", sa.Text),
    sa.column("evidence_required", sa.Text),
    sa.column("response_kind", sa.Text),
    sa.column("content_mix", sa.Text),
    sa.column("default_permission", sa.Text),
    sa.column("message_angle", sa.Text),
    sa.column("channel", sa.Text),
    sa.column("money_ceiling_kes", sa.Float),
    sa.column("paused", sa.Boolean),
    sa.column("valid_from", sa.Date),
    sa.column("valid_to", sa.Date),
)

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


def _carried_over(bind) -> list[ActionSpec]:
    """Every action in force today, with only the two live ones left running.

    Mirrors active_action_catalog_version's own selection: the single
    version with the latest valid_from that has started and not ended, not
    every row that happens to still be open, since an earlier version's rows
    are often left open rather than explicitly closed.
    """
    active_version = bind.execute(
        sa.select(agent_action_catalog.c.version)
        .where(
            agent_action_catalog.c.valid_from <= _VALID_FROM,
            sa.or_(
                agent_action_catalog.c.valid_to.is_(None),
                agent_action_catalog.c.valid_to > _VALID_FROM,
            ),
        )
        .order_by(agent_action_catalog.c.valid_from.desc(), agent_action_catalog.c.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if active_version is None:
        return []

    rows = bind.execute(
        sa.select(agent_action_catalog).where(agent_action_catalog.c.version == active_version)
    ).all()
    specs = []
    for row in rows:
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
    bind = op.get_bind()
    op.bulk_insert(
        agent_action_catalog,
        [
            {
                "version": _VERSION,
                "action_code": spec.action_code,
                "title": spec.title,
                "who": spec.who,
                "evidence_required": spec.evidence_required,
                "response_kind": spec.response_kind,
                "content_mix": spec.content_mix,
                "default_permission": spec.default_permission,
                "message_angle": spec.message_angle,
                "channel": spec.channel,
                "money_ceiling_kes": spec.money_ceiling_kes,
                "paused": spec.paused,
                "valid_from": _VALID_FROM,
                "valid_to": None,
            }
            for spec in _carried_over(bind)
        ],
    )
    op.execute(
        agent_action_catalog.update()
        .where(agent_action_catalog.c.version < _VERSION, agent_action_catalog.c.valid_to.is_(None))
        .values(valid_to=_VALID_FROM)
    )


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
