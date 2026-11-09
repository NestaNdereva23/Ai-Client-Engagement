from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog
from app.db.models.outreach import Campaign

OPEN_ENROLLMENT_STATUSES = ("enrolled", "in_progress")


def last_touch_sent_at(session: Session, campaign_id: int) -> datetime | None:
    return session.scalar(
        select(func.max(TouchLog.sent_at))
        .join(Enrollment, Enrollment.enrollment_id == TouchLog.enrollment_id)
        .where(Enrollment.campaign_id == campaign_id)
    )


def open_enrollment_count(session: Session, campaign_id: int) -> int:
    return (
        session.scalar(
            select(func.count()).where(
                Enrollment.campaign_id == campaign_id,
                Enrollment.status.in_(OPEN_ENROLLMENT_STATUSES),
            )
        )
        or 0
    )


def completes_at(session: Session, campaign: Campaign) -> datetime | None:
    if open_enrollment_count(session, campaign.campaign_id):
        return None
    last_sent = last_touch_sent_at(session, campaign.campaign_id)
    if last_sent is None:
        return None
    return last_sent + timedelta(days=get_settings().campaign_cooldown_days)


def complete_if_finished(session: Session, campaign: Campaign, now: datetime | None = None) -> bool:
    if campaign.status != "running":
        return False
    due = completes_at(session, campaign)
    if due is None or (now or datetime.now(UTC)) < due:
        return False
    campaign.status = "completed"
    session.flush()
    record_audit(
        session,
        entity_type="campaign",
        action="status_change",
        entity_id=str(campaign.campaign_id),
        detail={"from": "running", "to": "completed", "reason": "cooldown_after_last_touch"},
    )
    return True


def complete_finished_campaigns(session: Session, now: datetime | None = None) -> list[int]:
    running = session.scalars(select(Campaign).where(Campaign.status == "running")).all()
    return [c.campaign_id for c in running if complete_if_finished(session, c, now)]


def _progress_bucket(status: str) -> str:
    if status == "enrolled":
        return "waiting"
    if status == "in_progress":
        return "in_progress"
    if status == "completed":
        return "finished"
    return "stopped"


def _expected_complete_at(
    open_rows: list[tuple[int, datetime | None]], offsets: dict[int, int], cooldown: int
) -> datetime | None:
    if not open_rows or not offsets:
        return None
    last_offset = max(offsets.values())
    estimates = []
    for current_step, next_due_at in open_rows:
        if next_due_at is None:
            return None
        remaining = last_offset - offsets.get(current_step + 1, last_offset)
        estimates.append(next_due_at + timedelta(days=max(remaining, 0) + cooldown))
    return max(estimates)


def campaign_progress(session: Session, campaign: Campaign) -> dict:
    cooldown = get_settings().campaign_cooldown_days
    offsets = dict(
        session.execute(
            select(CampaignStep.step_no, CampaignStep.offset_days).where(
                CampaignStep.campaign_id == campaign.campaign_id
            )
        ).all()
    )
    rows = session.execute(
        select(Enrollment.status, Enrollment.current_step, Enrollment.next_due_at).where(
            Enrollment.campaign_id == campaign.campaign_id
        )
    ).all()
    counts = {"waiting": 0, "in_progress": 0, "finished": 0, "stopped": 0}
    for status, _, _ in rows:
        counts[_progress_bucket(status)] += 1
    open_rows = [(step, due) for status, step, due in rows if status in OPEN_ENROLLMENT_STATUSES]
    due_dates = [due for _, due in open_rows if due is not None]
    finish = completes_at(session, campaign)
    return {
        "step_days": [offsets[step] for step in sorted(offsets)],
        "cooldown_days": cooldown,
        "plan_length_days": (max(offsets.values()) if offsets else 0) + cooldown,
        "clients": counts,
        "next_send_at": min(due_dates) if due_dates else None,
        "last_sent_at": last_touch_sent_at(session, campaign.campaign_id),
        "expected_complete_at": finish or _expected_complete_at(open_rows, offsets, cooldown),
    }
