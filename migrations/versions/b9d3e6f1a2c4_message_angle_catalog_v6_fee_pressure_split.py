"""message_angle_catalog v6: briefs for the two fee-pressure angles

Revision ID: b9d3e6f1a2c4
Revises: a4f7c2e9b8d5
Create Date: 2026-09-16 13:05:00.000000

Splits fee_warning into two angles: one for a client who has gone quiet as
well as run into fee pressure, one for a client who is still paying in
despite it. The second must never read like a warning, since that would
contradict the fact that the client is still contributing.

Every angle already in force is carried over word for word, family included,
so this version only adds.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.rules.catalog import AngleSpec, load_active_angles, save_catalog_version

revision: str = "b9d3e6f1a2c4"
down_revision: str | Sequence[str] | None = "a4f7c2e9b8d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 6
_PREVIOUS = 5
_VALID_FROM = date(2026, 9, 16)

_NEW_ANGLES = [
    AngleSpec(
        angle="fee_pressure_warning_dormant",
        headline="Balance running down, and gone quiet",
        who=(
            "Someone whose balance covers only a few more months of the monthly fee, "
            "and who has not paid in for a while"
        ),
        claim=(
            "The monthly fee is slowly using up what is left, there has been no "
            "recent deposit, and on the way things are going the balance reaches "
            "zero in a named month"
        ),
        ask=(
            "Invite the client to add to the account, or to talk to us, before that month arrives"
        ),
        never=(
            "Never state the balance, the fee, or how many months are left as a "
            "number. Never say the account is empty, nearly empty, or closed. "
            "Never blame the client. Never promise a return or a rate. Never ask "
            "the client to confirm contact details"
        ),
        use=(
            "The month the balance is on course to reach zero is supplied as a "
            "fact, and this is one of the two angles allowed to name it. Write it "
            "exactly as it was supplied and add nothing to it. Do not work out any "
            "other date, count, or amount from it"
        ),
        family="fit_and_guidance",
    ),
    AngleSpec(
        angle="fee_pressure_encourage_active",
        headline="Still contributing, worth a nudge",
        who=(
            "Someone whose balance covers only a few more months of the monthly "
            "fee, who is still paying in"
        ),
        claim=(
            "The client is continuing to pay in even though the monthly fee eats "
            "into it, and keeping that up matters more than the fee does"
        ),
        ask=(
            "Encourage the client to keep contributing, and note that a slightly "
            "larger regular amount would outpace the fee"
        ),
        never=(
            "Never say or imply the account is at risk, running out, or in "
            "trouble, since the client is still contributing. Never state the "
            "balance, the fee, or how many months are left as a number. Never "
            "blame the client. Never promise a return or a rate. Never ask the "
            "client to confirm contact details"
        ),
        use=(
            "A current product fact may be used only when it makes the next step "
            "clearer. Do not state any figure that was not supplied as a fact"
        ),
        family="fit_and_guidance",
    ),
]


def _carried_over(session: Session) -> list[AngleSpec]:
    """Every angle in force today, exactly as it reads now."""
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
