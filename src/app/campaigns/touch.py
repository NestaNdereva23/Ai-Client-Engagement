"""Recording a touch, generating its message, sending it, and reconciling
touch_log against enrollment state if any of that gets interrupted.

A touch is logged in touch_log before anything else happens to it, under
the same (enrollment_id, step_no) key the schema already makes unique, so
a crash between logging and generating, or between generating and sending,
cannot turn into a second copy of the same touch on retry: the insert is a
no-op and the caller picks up wherever the previous attempt left off.
reconcile_enrollment catches an enrollment's current_step back up if a
send went out but the state machine never advanced it, the mirror image of
the same crash.

Generation and sending are both handed to the caller as small callables
rather than built in here, so this module has no dependency on an LLM
provider or an email provider: generate turns an enrollment and a step
into an OutreachMessage, sender turns an approved message into a delivery
outcome. The default sender is a stub that records a send without
delivering anything
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.campaigns.eligibility import check_eligibility, check_stop_conditions
from app.campaigns.scheduler import (
    DEFAULT_BATCH_LIMIT,
    advance_enrollment,
    count_stale_contacts,
    select_due_enrollments,
)
from app.db.models.campaigns import Enrollment, TouchLog
from app.db.models.outreach import OutreachMessage

logger = structlog.get_logger(__name__)

GenerateFn = Callable[[Session, Enrollment, int], OutreachMessage]


class SendBlocked(Exception):
    """Raised when the send-time recheck finds a reason not to deliver after all."""

    def __init__(self, reason: str | None) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SendResult:
    delivery_status: str
    sent_at: datetime


SenderFn = Callable[[OutreachMessage], SendResult]


def stub_sender(message: OutreachMessage) -> SendResult:
    """The send stub: marks a touch handled without delivering anything.

    Real delivery is a later milestone; this exists so the review-to-touch
    path can be exercised end to end without a provider.
    """
    return SendResult(delivery_status="stubbed", sent_at=datetime.now(UTC))


@dataclass(frozen=True)
class TouchRunOutcome:
    """What happened to one due enrollment during a batch run."""

    enrollment_id: int
    generated: bool
    reason: str | None = None
    touch_id: int | None = None


def record_touch(session: Session, enrollment: Enrollment, step_no: int) -> TouchLog:
    """Insert the touch_log row for (enrollment, step_no), or return the existing one.

    Safe to call more than once for the same step: the unique constraint
    makes a repeat a no-op rather than a second row, so retrying after a
    crash finds the same row instead of duplicating it.
    """
    stmt = pg_insert(TouchLog).values(enrollment_id=enrollment.enrollment_id, step_no=step_no)
    stmt = stmt.on_conflict_do_nothing(constraint="uq_touch_log_enrollment_step")
    session.execute(stmt)
    session.flush()
    return session.execute(
        select(TouchLog).where(
            TouchLog.enrollment_id == enrollment.enrollment_id, TouchLog.step_no == step_no
        )
    ).scalar_one()


def generate_touch(
    session: Session, enrollment: Enrollment, step_no: int, *, generate: GenerateFn
) -> TouchLog:
    """Log the touch and generate its message, unless this step is already logged.

    The touch_log row is written first, before generate runs, so a crash
    mid-generation leaves a touch row with no message yet rather than a
    message nothing recorded happening for; retrying finds the same row
    and can pick generation back up instead of creating a second touch for
    the same step.
    """
    touch = record_touch(session, enrollment, step_no)
    if touch.message_id is not None:
        return touch

    message = generate(session, enrollment, step_no)
    touch.message_id = message.message_id
    session.flush()
    return touch


def run_due_enrollments(
    session: Session,
    *,
    campaign_id: int | None = None,
    limit: int = DEFAULT_BATCH_LIMIT,
    generate: GenerateFn,
) -> list[TouchRunOutcome]:
    """Select due enrollments, gate each one, and generate a touch for those eligible.

    Nothing here sends anything: an eligible enrollment ends this run with
    a pending_review message and an unadvanced current_step, exactly where
    the review queue picks it up. Sending and advancing happen in
    send_touch, once a human approves.
    """
    due = select_due_enrollments(session, campaign_id=campaign_id, limit=limit)
    stale = count_stale_contacts(session, due)
    if stale:
        logger.info("run_due_enrollments.stale_contacts", stale=stale, batch=len(due))

    outcomes = []
    for enrollment in due:
        result = check_eligibility(session, enrollment)
        if not result.eligible:
            outcomes.append(
                TouchRunOutcome(enrollment.enrollment_id, generated=False, reason=result.reason)
            )
            continue

        step_no = enrollment.current_step + 1
        touch = generate_touch(session, enrollment, step_no, generate=generate)
        outcomes.append(
            TouchRunOutcome(enrollment.enrollment_id, generated=True, touch_id=touch.touch_id)
        )
    return outcomes


def send_touch(session: Session, touch: TouchLog, *, sender: SenderFn = stub_sender) -> TouchLog:
    """Send an approved touch's message, then advance its enrollment.

    Refuses a touch with no message yet or one not approved: nothing sends
    without human review even here. Re-checks the stop conditions right
    before sending and raises SendBlocked rather than delivering if
    something changed in the gap between approval and send.
    """
    if touch.message_id is None:
        raise ValueError("touch has no message to send yet")
    message = session.get(OutreachMessage, touch.message_id)
    if message is None or message.status != "approved":
        raise ValueError("only an approved message can be sent")

    enrollment = session.get(Enrollment, touch.enrollment_id)
    recheck = check_stop_conditions(session, enrollment)
    if not recheck.eligible:
        raise SendBlocked(recheck.reason)

    result = sender(message)
    touch.sent_at = result.sent_at
    touch.delivery_status = result.delivery_status
    session.flush()

    record_audit(
        session,
        entity_type="touch_log",
        action="send",
        entity_id=str(touch.touch_id),
        detail={"delivery_status": result.delivery_status},
    )

    advance_enrollment(session, enrollment, step_no=touch.step_no, sent_at=touch.sent_at)
    return touch


def reconcile_enrollment(session: Session, enrollment: Enrollment) -> Enrollment:
    """Catch current_step up to what touch_log shows actually sent.

    A no-op when they already agree. Only ever moves current_step forward,
    the same direction advance_enrollment already only moves, so this
    cannot undo a real transition, only complete one that was interrupted.
    """
    latest_sent = session.execute(
        select(TouchLog)
        .where(TouchLog.enrollment_id == enrollment.enrollment_id, TouchLog.sent_at.isnot(None))
        .order_by(TouchLog.step_no.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_sent is None or latest_sent.step_no <= enrollment.current_step:
        return enrollment
    return advance_enrollment(
        session, enrollment, step_no=latest_sent.step_no, sent_at=latest_sent.sent_at
    )
