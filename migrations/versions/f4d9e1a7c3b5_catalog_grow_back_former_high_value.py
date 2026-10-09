"""message_angle_catalog: grow_back and former_high_value

Revision ID: f4d9e1a7c3b5
Revises: e6a2c8f4b1d7
Create Date: 2026-10-06 11:30:00.000000

"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.rules import MessageAngleCatalog as M
from app.rules.catalog import AngleSpec, load_active_angles, save_catalog_version

revision: str = "f4d9e1a7c3b5"
down_revision: str | Sequence[str] | None = "e6a2c8f4b1d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 10, 6)

_NEW_ANGLES = [
    AngleSpec(
        angle="grow_back",
        headline="A little more and it covers itself",
        who=(
            "Someone whose balance sits below the safe line but still has a "
            "real amount left, within reach of covering its own monthly cost again"
        ),
        claim=(
            "Topping up by a modest amount would put the balance back above the "
            "point where it comfortably covers its own monthly cost"
        ),
        ask=(
            "Invite the client to add a modest amount when it suits them, framed "
            "as reaching that point rather than catching up"
        ),
        never=(
            "Never state the balance, the fee, or how much more is needed as a "
            "number. Never say the account is at risk, in trouble, or nearly "
            "empty. Never blame the client or suggest they have been neglectful. "
            "Never promise a return or a rate. Never ask the client to confirm "
            "contact details"
        ),
        use=(
            "Keep the tone opportunity rather than rescue: this is a client close "
            "to a good place, not one in danger. A current product fact may be "
            "used only when it makes the next step clearer. Do not state any "
            "figure that was not supplied as a fact"
        ),
    ),
    AngleSpec(
        angle="former_high_value",
        headline="They built something real here",
        who=(
            "Someone who paid in significant amounts over their history but now "
            "holds a small, shrinking balance"
        ),
        claim=(
            "They built a real position here over time, and picking it back up is straightforward"
        ),
        ask=(
            "Invite the client to speak with their advisor or add to the "
            "account, in a tone that recognises what they built rather than "
            "warning them"
        ),
        never=(
            "Never state the balance, what they paid in historically, or any "
            "amount or date. Never say the account is at risk, abandoned, or a "
            "mistake. Never blame the client. Never promise a return or a rate. "
            "Never ask the client to confirm contact details"
        ),
        use=(
            "This client's own account manager should be treated as a real part "
            "of the next step, not just copied for the record. A current "
            "product fact may be used only when it makes the next step clearer. "
            "Do not state any figure that was not supplied as a fact"
        ),
    ),
]


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
    if "grow_back" in active and "former_high_value" in active:
        return

    next_version = (session.scalar(select(func.max(M.version))) or 0) + 1
    save_catalog_version(
        session, next_version, [*_carried(active), *_NEW_ANGLES], valid_from=_VALID_FROM
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
            a="grow_back"
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
