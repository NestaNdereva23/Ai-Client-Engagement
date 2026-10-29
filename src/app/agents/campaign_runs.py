from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.campaign_agent_run import SUGGEST_RUN, CampaignAgentRun

logger = structlog.get_logger(__name__)

STALE_REASON = "the run did not finish in time and was marked failed"


class CampaignRunInProgress(Exception):
    pass


def fail_stale_campaign_runs(session: Session) -> list[int]:
    minutes = get_settings().campaign_agent_run_stale_after_minutes
    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    stale = session.scalars(
        select(CampaignAgentRun).where(
            CampaignAgentRun.state == "running", CampaignAgentRun.started_at < cutoff
        )
    ).all()
    for run in stale:
        run.state = "failed"
        run.finished_at = datetime.now(UTC)
        run.failure_reason = STALE_REASON
        record_audit(
            session,
            entity_type="campaign_agent_run",
            action="failed",
            entity_id=str(run.run_id),
            detail={"reason": "stale", "after_minutes": minutes},
        )
    if stale:
        session.commit()
        logger.info("campaign_agent.stale_runs_failed", run_ids=[run.run_id for run in stale])
    return [run.run_id for run in stale]


def start_campaign_run(
    session: Session, *, kind: str, campaign_id: int | None = None, trigger: str = "manual"
) -> CampaignAgentRun:
    fail_stale_campaign_runs(session)
    running = session.scalar(
        select(CampaignAgentRun.run_id)
        .where(
            CampaignAgentRun.state == "running",
            CampaignAgentRun.kind == kind,
            CampaignAgentRun.campaign_id.is_(None)
            if campaign_id is None
            else CampaignAgentRun.campaign_id == campaign_id,
        )
        .limit(1)
    )
    if running is not None:
        raise CampaignRunInProgress(f"run {running} is already going")

    run = CampaignAgentRun(kind=kind, campaign_id=campaign_id, trigger=trigger)
    session.add(run)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise CampaignRunInProgress("a run is already going") from None
    logger.info("campaign_agent.run_started", run_id=run.run_id, kind=kind, campaign=campaign_id)
    return run


def latest_suggestion_run(session: Session) -> CampaignAgentRun | None:
    fail_stale_campaign_runs(session)
    return session.scalar(
        select(CampaignAgentRun)
        .where(CampaignAgentRun.kind == SUGGEST_RUN)
        .order_by(CampaignAgentRun.run_id.desc())
        .limit(1)
    )
