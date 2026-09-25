from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import CompoundSelect, delete, func, select
from sqlalchemy.orm import Session

from app.db.models.models import IngestionStatus, RawStaging
from app.db.models.risk import RiskRun, RiskSnapshot
from app.db.models.signals import ClientSignalSnapshot, ClientSituationSnapshot, SignalRun

RAW_STAGING_RUNS_TO_KEEP = 2
SIGNAL_RUNS_TO_KEEP = 2
RISK_RUNS_TO_KEEP = 14

logger = structlog.get_logger(__name__)


def _stale_pull_run_ids(keep: int) -> CompoundSelect:
    completed = (
        select(
            IngestionStatus.run_id,
            IngestionStatus.endpoint,
            IngestionStatus.started_at,
            func.row_number()
            .over(partition_by=IngestionStatus.endpoint, order_by=IngestionStatus.started_at.desc())
            .label("position"),
        )
        .where(IngestionStatus.state == "completed")
        .subquery()
    )
    older_completed = select(completed.c.run_id).where(completed.c.position > keep)
    older_failed = (
        select(IngestionStatus.run_id)
        .join(completed, completed.c.endpoint == IngestionStatus.endpoint)
        .where(
            completed.c.position == 1,
            IngestionStatus.state == "failed",
            IngestionStatus.started_at < completed.c.started_at,
        )
    )
    return older_completed.union(older_failed)


def _delete_snapshots_of_older_runs(
    session: Session, run_model: Any, snapshot_models: tuple[Any, ...], keep: int
) -> int:
    kept_runs = (
        select(run_model.run_id)
        .where(run_model.state == "completed")
        .order_by(run_model.started_at.desc())
        .limit(keep)
    )
    stale_runs = select(run_model.run_id).where(
        run_model.state != "running", run_model.run_id.not_in(kept_runs)
    )
    return sum(
        session.execute(delete(model).where(model.run_id.in_(stale_runs))).rowcount
        for model in snapshot_models
    )


def prune_raw_staging(session: Session, keep: int = RAW_STAGING_RUNS_TO_KEEP) -> int:
    deleted = session.execute(
        delete(RawStaging).where(RawStaging.run_id.in_(_stale_pull_run_ids(keep)))
    ).rowcount
    logger.info("retention.pruned", table="raw_staging", rows=deleted, keep=keep)
    return deleted


def prune_signal_snapshots(session: Session, keep: int = SIGNAL_RUNS_TO_KEEP) -> int:
    deleted = _delete_snapshots_of_older_runs(
        session, SignalRun, (ClientSignalSnapshot, ClientSituationSnapshot), keep
    )
    logger.info("retention.pruned", table="signal_and_situation_snapshots", rows=deleted, keep=keep)
    return deleted


def prune_risk_snapshots(session: Session, keep: int = RISK_RUNS_TO_KEEP) -> int:
    deleted = _delete_snapshots_of_older_runs(session, RiskRun, (RiskSnapshot,), keep)
    logger.info("retention.pruned", table="risk_snapshot", rows=deleted, keep=keep)
    return deleted
