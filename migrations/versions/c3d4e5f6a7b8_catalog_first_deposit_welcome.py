from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.rules import MessageAngleCatalog as M
from app.rules.catalog import AngleSpec, load_active_angles, save_catalog_version

revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 10, 5)
_ANGLE = "first_deposit_welcome"

_NEW_ANGLE = AngleSpec(
    angle=_ANGLE,
    headline="Ready when you are to make a start",
    who="Someone who opened an account but has not yet paid any money into it",
    claim=(
        "Their account is open and ready, and making a first deposit to put it to "
        "work is simple and can be whatever amount suits them"
    ),
    ask="Invite the client to make their first deposit when it suits them, with no pressure",
    never=(
        "Never say or imply the client has gone quiet, lapsed, delayed, or done "
        "anything wrong, since they have not started yet. Never state any amount, "
        "date, or count. Never say how long the account has been open, that it is "
        "empty, or that it is at risk. Never promise a return or a rate. Never ask "
        "the client to confirm contact details"
    ),
    use=(
        "Keep it short and welcoming. A current product fact may be used only when "
        "it makes the first step clearer. Do not state any figure that was not "
        "supplied as a fact"
    ),
    family="onboarding",
)


def _carried(active) -> list[AngleSpec]:
    return [
        AngleSpec(
            angle=row.angle,
            headline=row.headline,
            who=row.who,
            claim=row.claim,
            ask=row.ask,
            never=row.never,
            use=row.use,
            held=row.held,
            cta=row.cta,
            family=row.family,
            tone=row.tone,
        )
        for row in active.values()
    ]


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    active = load_active_angles(session, _VALID_FROM)
    if _ANGLE in active:
        return

    next_version = (session.scalar(select(func.max(M.version))) or 0) + 1
    save_catalog_version(
        session, next_version, [*_carried(active), _NEW_ANGLE], valid_from=_VALID_FROM
    )
    session.flush()
    op.execute(
        sa.text(
            "UPDATE message_angle_catalog SET valid_to = :vf "
            "WHERE valid_to IS NULL AND version <> :v"
        ).bindparams(vf=_VALID_FROM, v=next_version)
    )


def downgrade() -> None:
    bind = op.get_bind()
    version = bind.execute(
        sa.text("SELECT max(version) FROM message_angle_catalog WHERE angle = :a").bindparams(
            a=_ANGLE
        )
    ).scalar()
    if version is None:
        return
    valid_from = bind.execute(
        sa.text("SELECT min(valid_from) FROM message_angle_catalog WHERE version = :v").bindparams(
            v=version
        )
    ).scalar()
    op.execute(
        sa.text("DELETE FROM message_angle_catalog WHERE version = :v").bindparams(v=version)
    )
    if valid_from is not None:
        op.execute(
            sa.text(
                "UPDATE message_angle_catalog SET valid_to = NULL WHERE valid_to = :vf"
            ).bindparams(vf=valid_from)
        )
