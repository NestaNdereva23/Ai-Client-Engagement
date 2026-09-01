from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.campaigns.batch_generation import ingest_batch
from app.config import Settings, get_settings
from app.db.models.generation_batch import GenerationBatch
from app.db.session import SessionLocal
from app.llmops.tracing import NullTracer, Tracer

logger = structlog.get_logger(__name__)

IN_FLIGHT_STATUSES = ("submitted", "in_progress")

POLL_WINDOW = timedelta(hours=6)
POLL_INTERVAL_SECONDS = 3 * 60


def poll_batch_until_done(
    generation_batch_id: str,
    *,
    settings: Settings | None = None,
    tracer: Tracer | None = None,
    session_factory: sessionmaker[Session] = SessionLocal,
    interval_seconds: float = POLL_INTERVAL_SECONDS,
    window: timedelta = POLL_WINDOW,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    settings = settings or get_settings()
    tracer = tracer or NullTracer()
    max_checks = max(1, int(window.total_seconds() // interval_seconds))

    for _ in range(max_checks):
        sleep(interval_seconds)
        with session_factory() as session:
            try:
                result = ingest_batch(
                    session, generation_batch_id, settings=settings, tracer=tracer
                )
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("batch_poll.check_failed", generation_batch_id=generation_batch_id)
                continue

        if result.batch.status not in IN_FLIGHT_STATUSES:
            logger.info(
                "batch_poll.stopped",
                generation_batch_id=generation_batch_id,
                status=result.batch.status,
            )
            return

    logger.info("batch_poll.window_elapsed", generation_batch_id=generation_batch_id)


@dataclass(frozen=True)
class BatchTickOutcome:
    """What happened to one batch during one tick."""

    generation_batch_id: str
    campaign_id: int
    status: str
    accepted: int
    rejected: int


@dataclass(frozen=True)
class BatchTickResult:
    """What one tick did, logged and returned to the caller."""

    considered: int
    ingested: int
    still_in_progress: int
    stale: int
    failed: int
    batches: list[BatchTickOutcome] = field(default_factory=list)


def run_batch_ingest_tick(
    session: Session,
    *,
    settings: Settings | None = None,
    tracer: Tracer | None = None,
) -> BatchTickResult:
    settings = settings or get_settings()
    tracer = tracer or NullTracer()
    now = datetime.now(UTC)

    in_flight = list(
        session.scalars(
            select(GenerationBatch).where(GenerationBatch.status.in_(IN_FLIGHT_STATUSES))
        )
    )
    due_ids: list[str] = []
    stale = 0
    for batch in in_flight:
        if batch.submitted_at is not None and now - batch.submitted_at > POLL_WINDOW:
            stale += 1
            continue
        due_ids.append(batch.generation_batch_id)

    outcomes: list[BatchTickOutcome] = []
    ingested = 0
    still_in_progress = 0
    failed = 0
    for generation_batch_id in due_ids:
        try:
            result = ingest_batch(session, generation_batch_id, settings=settings, tracer=tracer)
            session.commit()
        except Exception:
            session.rollback()
            failed += 1
            logger.exception("batch_ingest_tick.failed", generation_batch_id=generation_batch_id)
            continue

        outcomes.append(
            BatchTickOutcome(
                generation_batch_id=result.batch.generation_batch_id,
                campaign_id=result.batch.campaign_id,
                status=result.batch.status,
                accepted=sum(1 for o in result.outcomes if o.status == "accepted"),
                rejected=sum(1 for o in result.outcomes if o.status == "rejected"),
            )
        )
        if result.batch.status == "ingested":
            ingested += 1
        elif result.batch.status in IN_FLIGHT_STATUSES:
            still_in_progress += 1

    result_summary = BatchTickResult(
        considered=len(due_ids),
        ingested=ingested,
        still_in_progress=still_in_progress,
        stale=stale,
        failed=failed,
        batches=outcomes,
    )
    logger.info(
        "batch_ingest_tick.completed",
        considered=result_summary.considered,
        ingested=result_summary.ingested,
        still_in_progress=result_summary.still_in_progress,
        stale=result_summary.stale,
        failed=result_summary.failed,
    )
    return result_summary
