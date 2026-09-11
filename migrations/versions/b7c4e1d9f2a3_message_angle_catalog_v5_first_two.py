"""message_angle_catalog v5: a brief for each of the first two live actions

Revision ID: b7c4e1d9f2a3
Revises: a1f5c60d28b7
Create Date: 2026-09-09 14:00:00.000000

The two actions that go live need briefs of their own. Neither fits an
existing one: the welcome is for someone who has just arrived rather than
someone who left, and the fee warning is the only angle allowed to name a
month, because the month the balance runs out is the whole point of it.

Every angle already in force is carried over word for word by reading it
back, so this version only adds.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.rules.catalog import AngleSpec, load_active_angles, save_catalog_version

revision: str = "b7c4e1d9f2a3"
down_revision: str | Sequence[str] | None = "a1f5c60d28b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 5
_PREVIOUS = 4
_VALID_FROM = date(2026, 9, 9)

_NEW_ANGLES = [
    AngleSpec(
        angle="welcome_and_top_up",
        headline="A good time to add a little",
        who=(
            "Someone who joined recently, has paid in once, and holds a balance "
            "small enough that the monthly fee matters to them"
        ),
        claim=(
            "They have made a start, their account is open and ready, and adding "
            "a little more to it is simple"
        ),
        ask="Invite the client to add a small amount when it suits them, with no pressure",
        never=(
            "Never say or imply the client has gone quiet, lapsed, or done "
            "anything wrong. Never state their balance, what they paid in, when "
            "they joined, or how many times they have paid in. Never mention the "
            "monthly fee. Never promise a return or a rate. Never ask the client "
            "to confirm contact details"
        ),
        use=(
            "Keep it short and welcoming. A current product fact may be used only "
            "when it makes the next step clearer. Do not state any figure that was "
            "not supplied as a fact"
        ),
    ),
    AngleSpec(
        angle="fee_warning",
        headline="Your balance is running down",
        who="Someone whose balance covers only a few more months of the monthly fee",
        claim=(
            "The monthly fee is slowly using up what is left, and on the way "
            "things are going the balance reaches zero in a named month"
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
            "fact, and this is the one angle allowed to name it. Write it exactly "
            "as it was supplied and add nothing to it. Do not work out any other "
            "date, count, or amount from it"
        ),
    ),
]

message_angle_catalog = sa.table(
    "message_angle_catalog",
    sa.column("version", sa.Integer),
    sa.column("valid_to", sa.Date),
)


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
        message_angle_catalog.update()
        .where(message_angle_catalog.c.version == _PREVIOUS)
        .values(valid_to=_VALID_FROM)
    )


def downgrade() -> None:
    op.execute(
        message_angle_catalog.update()
        .where(message_angle_catalog.c.version == _PREVIOUS)
        .values(valid_to=None)
    )
    op.execute(f"DELETE FROM message_angle_catalog WHERE version = {_VERSION}")
