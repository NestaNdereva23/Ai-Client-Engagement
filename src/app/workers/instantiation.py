from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.orm import Session, sessionmaker

from app.campaigns.instantiation import instantiate_many_templates
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT
from app.db.models.instantiation_batch import InstantiationBatch
from app.db.session import SessionLocal

logger = structlog.get_logger(__name__)


def run_instantiate_all_in_background(
    instantiation_batch_id: str,
    campaign_id: int,
    template_ids: list[str],
    *,
    limit: int = DEFAULT_BATCH_LIMIT,
    session_factory: sessionmaker[Session] = SessionLocal,
) -> None:
    with session_factory() as session:
        try:
            result = instantiate_many_templates(
                session, template_ids, campaign_id=campaign_id, limit=limit
            )
        except Exception:
            session.rollback()
            logger.exception(
                "instantiate_all.run_failed",
                instantiation_batch_id=instantiation_batch_id,
                campaign_id=campaign_id,
            )
            batch = session.get(InstantiationBatch, instantiation_batch_id)
            if batch is not None:
                batch.status = "failed"
                batch.completed_at = datetime.now(UTC)
                session.commit()
            return

        batch = session.get(InstantiationBatch, instantiation_batch_id)
        if batch is not None:
            batch.status = "completed"
            batch.instantiated_count = result.instantiated_count
            batch.failed_template_count = len(result.failed_template_ids)
            batch.completed_at = datetime.now(UTC)
            session.commit()
        logger.info(
            "instantiate_all.completed",
            instantiation_batch_id=instantiation_batch_id,
            campaign_id=campaign_id,
            instantiated_count=result.instantiated_count,
            failed_template_count=len(result.failed_template_ids),
        )
