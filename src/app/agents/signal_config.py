from __future__ import annotations

from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models.signals import SignalThreshold


def active_threshold(
    session: Session, signal_code: str, threshold_name: str, as_of: date
) -> float | None:
    return session.scalar(
        select(SignalThreshold.value)
        .where(
            SignalThreshold.signal_code == signal_code,
            SignalThreshold.threshold_name == threshold_name,
            SignalThreshold.valid_from <= as_of,
            or_(SignalThreshold.valid_to.is_(None), SignalThreshold.valid_to > as_of),
        )
        .order_by(SignalThreshold.valid_from.desc(), SignalThreshold.version.desc())
        .limit(1)
    )
