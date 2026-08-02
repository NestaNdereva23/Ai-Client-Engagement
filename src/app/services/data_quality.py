"""Data-quality reporting over one ingestion run: rejects, reasons, and shortfall."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.models import IngestionReject, IngestionStatus
from app.services.ingestion import RunNotFound

_MAX_REASONS = 20


class NoRunsYet(Exception):
    """No ingestion run has ever started; there is nothing to report on."""


def quality_summary(
    session: Session, *, run_id: str | None = None
) -> tuple[IngestionStatus, list[tuple[str, int]]]:
    """The given run, or the most recent one, with its reject reasons ranked by count.

    Raises RunNotFound when run_id names a run that does not exist, or
    NoRunsYet when none is given and no run has ever started.
    """
    if run_id is not None:
        run = session.get(IngestionStatus, run_id)
        if run is None:
            raise RunNotFound(run_id)
    else:
        run = session.scalar(
            select(IngestionStatus).order_by(IngestionStatus.started_at.desc()).limit(1)
        )
        if run is None:
            raise NoRunsYet()

    reasons = session.execute(
        select(IngestionReject.reason, func.count())
        .where(IngestionReject.run_id == run.run_id)
        .group_by(IngestionReject.reason)
        .order_by(func.count().desc())
        .limit(_MAX_REASONS)
    ).all()
    return run, [(reason, count) for reason, count in reasons]
