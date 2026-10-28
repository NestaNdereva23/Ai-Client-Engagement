from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.campaigns import ContactEvent, Enrollment, TouchLog
from app.db.models.outreach import Campaign

AGENT_ACTOR = "agent"
HARM_EVENT_TYPES = ("bounce", "optout")
WATCHED_STATUSES = ("draft", "running")


def _contacted_and_harmed(session: Session, campaign_id: int) -> tuple[int, int]:
    first_sent = (
        select(Enrollment.client_id, func.min(TouchLog.sent_at).label("first_sent"))
        .join(TouchLog, TouchLog.enrollment_id == Enrollment.enrollment_id)
        .where(Enrollment.campaign_id == campaign_id, TouchLog.sent_at.is_not(None))
        .group_by(Enrollment.client_id)
        .subquery()
    )
    contacted = session.scalar(select(func.count()).select_from(first_sent)) or 0
    harmed = (
        session.scalar(
            select(func.count(func.distinct(first_sent.c.client_id))).join(
                ContactEvent,
                (ContactEvent.client_id == first_sent.c.client_id)
                & (ContactEvent.type.in_(HARM_EVENT_TYPES))
                & (ContactEvent.occurred_at >= first_sent.c.first_sent),
            )
        )
        or 0
    )
    return contacted, harmed


def pause_if_unhealthy(session: Session, campaign: Campaign) -> str | None:
    if campaign.proposed_by_run_id is None or campaign.status not in WATCHED_STATUSES:
        return None
    settings = get_settings()
    contacted, harmed = _contacted_and_harmed(session, campaign.campaign_id)
    if contacted < settings.agent_campaign_pause_min_sent:
        return None
    share = harmed / contacted
    if share <= settings.agent_campaign_pause_share:
        return None

    reason = (
        f"{harmed} of {contacted} contacted clients bounced or opted out ({share:.0%}), "
        f"above the {settings.agent_campaign_pause_share:.0%} line"
    )
    campaign.status = "paused"
    session.flush()
    record_audit(
        session,
        entity_type="campaign",
        action="pause",
        entity_id=str(campaign.campaign_id),
        actor_id=AGENT_ACTOR,
        detail={"reason": reason, "contacted": contacted, "harmed": harmed},
    )
    return reason
