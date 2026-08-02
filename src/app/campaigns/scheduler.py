"""Selecting due enrollments and advancing them after a touch.

A fresh enrollment with no next_due_at yet is due immediately, so its first
touch does not wait on a scheduler cycle. After a touch, the gap to the next
one is measured from the step offsets: each campaign_step's offset_days is
the day count since the campaign started, so the wait before the next touch
is the difference between the next step's offset and the one just sent, laid
on top of when this touch actually went out rather than a fixed calendar
date. That way a touch sent late does not make the next one land even
later than it should relative to the sequence.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.campaigns.state_machine import transition_enrollment
from app.db.models.campaigns import CampaignStep, Enrollment

DEFAULT_BATCH_LIMIT = 500

_SCHEDULABLE_STATUSES = ("enrolled", "in_progress")


def select_due_enrollments(
    session: Session, *, campaign_id: int | None = None, limit: int = DEFAULT_BATCH_LIMIT
) -> list[Enrollment]:
    """Enrollments ready for their next touch, oldest enrollment first.

    Due means next_due_at has passed, or has never been set (nothing has
    been sent yet). A bounded limit keeps one run to a manageable batch;
    running again the same day just picks up whatever is still due, since
    this only reads and never changes anything itself.
    """
    query = select(Enrollment).where(
        Enrollment.status.in_(_SCHEDULABLE_STATUSES),
        (Enrollment.next_due_at.is_(None)) | (Enrollment.next_due_at <= func.now()),
    )
    if campaign_id is not None:
        query = query.where(Enrollment.campaign_id == campaign_id)
    query = query.order_by(Enrollment.enrollment_id).limit(limit)
    return list(session.execute(query).scalars())


def advance_enrollment(
    session: Session, enrollment: Enrollment, *, step_no: int, sent_at: datetime
) -> Enrollment:
    """Move an enrollment forward after step_no went out at sent_at.

    Safe to call again for a step already advanced past: it is then a
    no-op rather than skipping ahead a second time, so a scheduler run
    repeated for the same day cannot push an enrollment further than the
    touches it actually sent.
    """
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
        delta_days = next_step.offset_days - (current.offset_days if current else 0)
        enrollment.next_due_at = sent_at + timedelta(days=max(delta_days, 0))
        transition_enrollment(session, enrollment, to_status="in_progress", reason="touch_sent")
    else:
        enrollment.next_due_at = None
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
