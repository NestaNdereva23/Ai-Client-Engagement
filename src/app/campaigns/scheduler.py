"""Selecting due enrollments and advancing them after a touch.

A fresh enrollment with no next_due_at yet is due immediately, so its first
touch does not wait on a scheduler cycle. After a touch, the gap to the next
one is measured from the step offsets: each campaign_step's offset_days is
the day count since the campaign started, so the wait before the next touch
is the difference between the next step's offset and the one just sent.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.campaigns.state_machine import transition_enrollment
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog
from app.db.models.models import ClientFeatures

DEFAULT_BATCH_LIMIT = 500
MAX_BATCH_LIMIT = 10000

_SCHEDULABLE_STATUSES = ("enrolled", "in_progress")


def select_due_enrollments(
    session: Session, *, campaign_id: int | None = None, limit: int = DEFAULT_BATCH_LIMIT
) -> list[Enrollment]:

    next_step_no = Enrollment.current_step + 1
    query = (
        select(Enrollment)
        .outerjoin(ClientFeatures, ClientFeatures.client_id == Enrollment.client_id)
        .outerjoin(
            TouchLog,
            (TouchLog.enrollment_id == Enrollment.enrollment_id)
            & (TouchLog.step_no == next_step_no),
        )
        .where(
            Enrollment.status.in_(_SCHEDULABLE_STATUSES),
            Enrollment.is_primary_contact_row.is_(True),
            (Enrollment.next_due_at.is_(None)) | (Enrollment.next_due_at <= func.now()),
            TouchLog.touch_id.is_(None),
        )
    )
    if campaign_id is not None:
        query = query.where(Enrollment.campaign_id == campaign_id)
    query = query.order_by(
        func.coalesce(ClientFeatures.stale_contact, False), Enrollment.enrollment_id
    ).limit(limit)
    return list(session.execute(query).scalars())


def count_stale_contacts(session: Session, enrollments: Sequence[Enrollment]) -> int:
    client_ids = {e.client_id for e in enrollments}
    if not client_ids:
        return 0
    return (
        session.execute(
            select(func.count())
            .select_from(ClientFeatures)
            .where(ClientFeatures.client_id.in_(client_ids), ClientFeatures.stale_contact.is_(True))
        ).scalar_one()
        or 0
    )


def advance_enrollment(
    session: Session, enrollment: Enrollment, *, step_no: int, sent_at: datetime
) -> Enrollment:
    """Move an enrollment forward after step_no went out at sent_at."""
    if enrollment.current_step >= step_no:
        return enrollment

    steps = {
        row.step_no: row
        for row in session.execute(
            select(CampaignStep).where(CampaignStep.campaign_id == enrollment.campaign_id)
        ).scalars()
    }
    current = steps.get(step_no)
    next_step = steps.get(step_no + 1)

    enrollment.current_step = step_no
    if next_step is not None:
        print(f"Next step {next_step.step_no} is due in {next_step.offset_days} days")
        delta_days = next_step.offset_days - (current.offset_days if current else 0)
        enrollment.next_due_at = sent_at + timedelta(days=max(delta_days, 0))
        transition_enrollment(session, enrollment, to_status="in_progress", reason="touch_sent")
    else:
        enrollment.next_due_at = None
        if enrollment.status == "enrolled":
            transition_enrollment(session, enrollment, to_status="in_progress", reason="touch_sent")
        transition_enrollment(session, enrollment, to_status="completed", reason="all_touches_sent")
    session.flush()

    record_audit(
        session,
        entity_type="enrollment",
        action="advance",
        entity_id=str(enrollment.enrollment_id),
        detail={
            "step_no": step_no,
            "status": enrollment.status,
            "next_due_at": enrollment.next_due_at.isoformat() if enrollment.next_due_at else None,
        },
    )
    return enrollment
