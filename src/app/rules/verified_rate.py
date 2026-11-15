from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models.verified_rate import VerifiedRateFact


def record_verified_rate(
    session: Session, *, product: str, rate_value: float, source: str, recorded_by: str
) -> VerifiedRateFact:
    row = VerifiedRateFact(
        product=product,
        rate_value=rate_value,
        source=source,
        recorded_by=recorded_by,
        as_of=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def latest_verified_rate(session: Session, product: str) -> VerifiedRateFact | None:
    return session.execute(
        select(VerifiedRateFact)
        .where(VerifiedRateFact.product == product)
        .order_by(VerifiedRateFact.as_of.desc())
        .limit(1)
    ).scalar_one_or_none()


def get_verified_rate(
    session: Session, product: str, *, settings: Settings | None = None
) -> VerifiedRateFact | None:
    # None once the latest row has gone stale, not the stale row itself.
    row = latest_verified_rate(session, product)
    if row is None:
        return None
    settings = settings or get_settings()
    age_hours = (datetime.now(UTC) - row.as_of).total_seconds() / 3600
    return row if age_hours <= settings.verified_rate_max_age_hours else None
