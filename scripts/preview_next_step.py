from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.audit.log import record_audit  # noqa: E402
from app.campaigns.scheduler import SCHEDULABLE_STATUSES, advance_enrollment  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog  # noqa: E402
from app.db.models.outreach import Campaign, OutreachMessage  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402

SIMULATED = "simulated"


def _waiting_touch(session: Session, enrollment: Enrollment) -> TouchLog | None:
    return session.scalar(
        select(TouchLog).where(
            TouchLog.enrollment_id == enrollment.enrollment_id,
            TouchLog.step_no == enrollment.current_step + 1,
            TouchLog.message_id.isnot(None),
            TouchLog.sent_at.is_(None),
        )
    )


def _step_exists(session: Session, campaign_id: int, step_no: int) -> bool:
    return (
        session.scalar(
            select(CampaignStep.step_id).where(
                CampaignStep.campaign_id == campaign_id, CampaignStep.step_no == step_no
            )
        )
        is not None
    )


def _fast_forward(session: Session, enrollment: Enrollment, sent_at: datetime) -> str:
    touch = _waiting_touch(session, enrollment)
    if touch is None:
        due = enrollment.next_due_at
        if due is not None and due > datetime.now(UTC):
            enrollment.next_due_at = func.now()
            record_audit(
                session,
                entity_type="enrollment",
                action="skip_wait",
                entity_id=str(enrollment.enrollment_id),
                detail={"step_no": enrollment.current_step, "was_due_at": due.isoformat()},
            )
            return (
                f"step {enrollment.current_step} already sent, wait skipped (was {due:%d %b %H:%M})"
            )
        return "no unsent draft waiting"
    message = session.get(OutreachMessage, touch.message_id)
    if message.status == "rejected":
        return f"step {touch.step_no} draft was rejected, left alone"
    if not _step_exists(session, enrollment.campaign_id, touch.step_no + 1):
        return f"step {touch.step_no} is the last step, left alone"

    message.status = "approved"
    touch.sent_at = sent_at
    touch.delivery_status = SIMULATED
    advance_enrollment(session, enrollment, step_no=touch.step_no, sent_at=sent_at, gap_days=0)
    record_audit(
        session,
        entity_type="touch_log",
        action="simulate_send",
        entity_id=str(touch.touch_id),
        detail={"step_no": touch.step_no, "enrollment_id": enrollment.enrollment_id},
    )
    return f"step {touch.step_no} marked as sent, ready for step {touch.step_no + 1}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mark unsent touches as sent and skip the wait for the next step."
    )
    parser.add_argument("--campaign-id", type=int, required=True)
    parser.add_argument("--force-live", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    sent_at = datetime.now(UTC) - timedelta(days=settings.campaign_cooldown_days + 1)

    with SessionLocal() as session:
        campaign = session.get(Campaign, args.campaign_id)
        if campaign is None:
            parser.error(f"no campaign with id {args.campaign_id}")
        if not campaign.is_test and not args.force_live:
            parser.error("campaign is not marked is_test; pass --force-live to use it anyway")

        enrollments = session.scalars(
            select(Enrollment)
            .where(
                Enrollment.campaign_id == args.campaign_id,
                Enrollment.status.in_(SCHEDULABLE_STATUSES),
            )
            .order_by(Enrollment.enrollment_id)
        )
        for enrollment in enrollments:
            result = _fast_forward(session, enrollment, sent_at)
            print(f"enrollment {enrollment.enrollment_id} client {enrollment.client_id}: {result}")
        session.commit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
