"""risk_config_version 2: a real withdrawal is anything above the KES 50 monthly fee
Everything else is carried over from the version in force."""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.risk.store import load_active_config, save_config_version

revision: str = "d8f1b3a5c7e2"
down_revision: str | Sequence[str] | None = "9a4d6f2c8e1b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 2
_PREVIOUS = 1
_VALID_FROM = date(2026, 10, 13)
_WITHDRAWAL_LINE_KES = 50


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    previous = load_active_config(session, _VALID_FROM)
    save_config_version(
        session,
        _VERSION,
        dict(previous.weights),
        {**previous.thresholds, "SYSTEM_FEE_MAX": _WITHDRAWAL_LINE_KES},
        fa_call_capacity=previous.fa_call_capacity,
        at_risk_min=previous.at_risk_min,
        digest_cap_per_group=previous.digest_cap_per_group,
        valid_from=_VALID_FROM,
    )
    previous.valid_to = _VALID_FROM
    session.flush()


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM risk_config_version WHERE version = :version").bindparams(
            version=_VERSION
        )
    )
    op.execute(
        sa.text(
            "UPDATE risk_config_version SET valid_to = NULL "
            "WHERE version = :previous AND valid_to = :valid_from"
        ).bindparams(previous=_PREVIOUS, valid_from=_VALID_FROM)
    )
    op.execute(
        sa.text(
            "UPDATE active_configuration SET active_version = :previous "
            "WHERE component_type = 'risk_config_version' AND active_version = :version"
        ).bindparams(previous=_PREVIOUS, version=_VERSION)
    )
