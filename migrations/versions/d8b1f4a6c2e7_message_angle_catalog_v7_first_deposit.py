from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.rules.catalog import AngleSpec, load_active_angles, save_catalog_version

revision: str = "d8b1f4a6c2e7"
down_revision: str | Sequence[str] | None = "7c3e750bad4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 7
_PREVIOUS = 6
_VALID_FROM = date(2026, 10, 5)

_NEW_ANGLES = [
    AngleSpec(
        angle="first_deposit_welcome",
        headline="Ready when you are to make a start",
        who="Someone who opened an account but has not yet paid any money into it",
        claim=(
            "Their account is open and ready, and making a first deposit to put it "
            "to work is simple and can be whatever amount suits them"
        ),
        ask=("Invite the client to make their first deposit when it suits them, with no pressure"),
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
    ),
]


def _carried_over(session: Session) -> list[AngleSpec]:
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
        for row in load_active_angles(session, _VALID_FROM).values()
    ]


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    save_catalog_version(
        session, _VERSION, [*_carried_over(session), *_NEW_ANGLES], valid_from=_VALID_FROM
    )
    session.flush()
    op.execute(
        sa.text(
            "UPDATE message_angle_catalog SET valid_to = :valid_from "
            "WHERE version = :previous AND valid_to IS NULL"
        ).bindparams(previous=_PREVIOUS, valid_from=_VALID_FROM)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE message_angle_catalog SET valid_to = NULL "
            "WHERE version = :previous AND valid_to = :valid_from"
        ).bindparams(previous=_PREVIOUS, valid_from=_VALID_FROM)
    )
    op.execute(
        sa.text("DELETE FROM message_angle_catalog WHERE version = :version").bindparams(
            version=_VERSION
        )
    )
