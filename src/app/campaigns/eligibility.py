"""The eligibility gate: whether an enrollment's next touch may go out.

Runs before generation and again right before send, since a suppression or
a reply can land in the gap between the two. Every skip is logged with its
reason. A skip that happens before the enrollment's first touch always
lands on excluded, matching the enrollment state machine, which only has
one way out of the enrolled state; a skip that happens mid sequence lands
on the specific stopped state for that reason. A skip caused by something
that might resolve itself later (a cooldown window, a paused campaign, a
touch still waiting on a decision) does not change the enrollment's status
at all, it is just tried again on the next run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.campaigns import CampaignStep, ContactEvent, Enrollment, TouchLog
from app.db.models.models import Clients, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.models.suppression import Suppression
from app.db.session import restricted_session

_UNRESOLVED_MESSAGE_STATUSES = ("pending_review", "escalated", "held")
_STOPPING_EVENT_TYPES = ("bounce", "complaint")


@dataclass(frozen=True)
class EligibilityResult:
    """Whether the next touch may go out, and why not if it may not."""

    eligible: bool
    reason: str | None = None
    detail: str | None = None


def check_eligibility(
    session: Session, enrollment: Enrollment, *, cooldown_days: int | None = None
) -> EligibilityResult:
    """Evaluate every skip condition for an enrollment's next touch, in order.

    The first condition that applies wins; nothing further is checked once
    a skip is decided.
    """
    campaign = session.get(Campaign, enrollment.campaign_id)
    if campaign is None or campaign.status in ("paused", "completed"):
        return _skip(session, enrollment, reason="campaign_inactive", terminal=False)

    step_no = enrollment.current_step + 1
    step = session.scalar(
        select(CampaignStep).where(
            CampaignStep.campaign_id == enrollment.campaign_id, CampaignStep.step_no == step_no
        )
    )
    if step is None:
        return _skip(session, enrollment, reason="no_next_step", terminal=False)

    if _already_touched(session, enrollment.enrollment_id, step_no):
        return _skip(session, enrollment, reason="already_touched", terminal=False)

    if _has_unresolved_touch(session, enrollment.enrollment_id):
        return _skip(session, enrollment, reason="previous_touch_pending", terminal=False)

    suppression_reason = session.get(Suppression, enrollment.client_id)
    if suppression_reason is not None:
        status = (
            "stopped_bounce" if "bounce" in suppression_reason.reason.lower() else "stopped_optout"
        )
        return _skip(
            session,
            enrollment,
            reason="suppressed",
            terminal=True,
            terminal_status=status,
            detail=suppression_reason.reason,
        )

    opted_out, has_contact = _vault_signals(enrollment.client_id)
    if opted_out:
        return _skip(
            session, enrollment, reason="opted_out", terminal=True, terminal_status="stopped_optout"
        )
    if not has_contact:
        return _skip(
            session,
            enrollment,
            reason="no_deliverable_contact",
            terminal=True,
            terminal_status="excluded",
        )

    days = get_settings().campaign_cooldown_days if cooldown_days is None else cooldown_days
    if _within_cooldown(session, enrollment.client_id, days):
        return _skip(session, enrollment, reason="cooldown", terminal=False)

    stopping_event = _latest_event_type(session, enrollment.client_id, _STOPPING_EVENT_TYPES)
    if stopping_event is not None:
        return _skip(
            session,
            enrollment,
            reason=stopping_event,
            terminal=True,
            terminal_status="stopped_bounce",
        )

    since = _last_touch_sent_at(session, enrollment)
    if _latest_event_type(session, enrollment.client_id, ("reply",), since=since) is not None:
        return _skip(
            session, enrollment, reason="replied", terminal=True, terminal_status="stopped_reply"
        )

    if _reengaged(session, enrollment):
        return _skip(
            session,
            enrollment,
            reason="reengaged",
            terminal=True,
            terminal_status="stopped_reengaged",
        )

    return EligibilityResult(eligible=True)


def _skip(
    session: Session,
    enrollment: Enrollment,
    *,
    reason: str,
    terminal: bool,
    terminal_status: str | None = None,
    detail: str | None = None,
) -> EligibilityResult:
    record_audit(
        session,
        entity_type="enrollment",
        action="gate_skip",
        entity_id=str(enrollment.enrollment_id),
        detail={"reason": reason, "detail": detail, "terminal": terminal},
    )
    if terminal:
        enrollment.status = "excluded" if enrollment.current_step == 0 else terminal_status
        session.flush()
    return EligibilityResult(eligible=False, reason=reason, detail=detail)


def _already_touched(session: Session, enrollment_id: int, step_no: int) -> bool:
    return (
        session.scalar(
            select(TouchLog.touch_id).where(
                TouchLog.enrollment_id == enrollment_id, TouchLog.step_no == step_no
            )
        )
        is not None
    )


def _has_unresolved_touch(session: Session, enrollment_id: int) -> bool:
    latest = session.scalar(
        select(TouchLog)
        .where(TouchLog.enrollment_id == enrollment_id)
        .order_by(TouchLog.step_no.desc())
        .limit(1)
    )
    if latest is None:
        return False
    if latest.message_id is None:
        return True
    message = session.get(OutreachMessage, latest.message_id)
    if message is None:
        return True
    if message.status in _UNRESOLVED_MESSAGE_STATUSES:
        return True
    return message.status == "approved" and latest.sent_at is None


def _vault_signals(client_id: int) -> tuple[bool, bool]:
    """(opted_out, has_deliverable_contact), read once under the restricted role."""
    with restricted_session() as session:
        vault = session.get(PiiVault, client_id)
        record_audit(
            session,
            entity_type="pii_vault",
            action="read",
            entity_id=str(client_id),
            detail={"purpose": "eligibility_gate"},
        )
        session.commit()
        if vault is None:
            return False, False
        return vault.opt_out_flag, bool(vault.contact_email or vault.contact_whatsapp)


def _within_cooldown(session: Session, client_id: int, cooldown_days: int) -> bool:
    cutoff = func.now() - timedelta(days=cooldown_days)
    row = session.execute(
        select(TouchLog.touch_id)
        .join(Enrollment, Enrollment.enrollment_id == TouchLog.enrollment_id)
        .where(
            Enrollment.client_id == client_id,
            TouchLog.sent_at.isnot(None),
            TouchLog.sent_at >= cutoff,
        )
        .limit(1)
    ).first()
    return row is not None


def _latest_event_type(
    session: Session, client_id: int, types: tuple[str, ...], *, since=None
) -> str | None:
    query = select(ContactEvent.type).where(
        ContactEvent.client_id == client_id, ContactEvent.type.in_(types)
    )
    if since is not None:
        query = query.where(ContactEvent.occurred_at > since)
    query = query.order_by(ContactEvent.occurred_at.desc()).limit(1)
    return session.scalar(query)


def _last_touch_sent_at(session: Session, enrollment: Enrollment):
    sent_at = session.scalar(
        select(func.max(TouchLog.sent_at)).where(TouchLog.enrollment_id == enrollment.enrollment_id)
    )
    return sent_at or enrollment.enrolled_at


def _reengaged(session: Session, enrollment: Enrollment) -> bool:
    client = session.get(Clients, enrollment.client_id)
    if client is None:
        return False
    if client.balance is not None and client.balance > 0:
        return True
    return bool(
        client.last_activity_date is not None
        and client.last_activity_date > enrollment.enrolled_at.date()
    )
