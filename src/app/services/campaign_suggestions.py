from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.agents.permissions import effective_permission
from app.audit.log import record_audit
from app.campaigns.health import AGENT_ACTOR
from app.campaigns.readiness import (
    CampaignNotReady,
    campaign_readiness_problems,
    require_campaign_ready,
)
from app.campaigns.waiting_pool import waiting_client_ids
from app.config import get_settings
from app.db.models.campaigns import CampaignStep
from app.db.models.outreach import (
    AGENT_PLANNING_MODE,
    SUGGESTION_APPROVED,
    SUGGESTION_DISMISSED,
    SUGGESTION_PENDING,
    Campaign,
)
from app.schemas.campaigns import CampaignDraftIn, SuggestedCampaignOut
from app.services.campaigns import (
    add_campaign_step,
    create_campaign_record,
    enroll_campaign_cohort,
)
from app.services.clients import resolve_cohort_client_ids

OPEN_CAMPAIGN_ACTION = "open_campaign"
ACT_ALONE = "act_alone"
LIVE_STATUSES = ("draft", "running")
SHAPE_FIELDS = ("name", "cohort", "planning_mode", "plan_config", "touch_offsets")


class SuggestionNotFound(Exception):
    pass


class SuggestionNotPending(Exception):
    pass


class NothingWaiting(Exception):
    pass


def _shape(draft: CampaignDraftIn) -> dict:
    return draft.model_dump(mode="json", exclude={"reason"})


def _write_steps(session: Session, campaign_id: int, offsets: list[int]) -> None:
    for offset in offsets:
        add_campaign_step(session, campaign_id, offset_days=offset)


def current_draft(session: Session, campaign: Campaign) -> CampaignDraftIn:
    offsets = session.scalars(
        select(CampaignStep.offset_days)
        .where(CampaignStep.campaign_id == campaign.campaign_id)
        .order_by(CampaignStep.step_no)
    ).all()
    return CampaignDraftIn(
        name=campaign.name,
        cohort=campaign.cohort_definition,
        planning_mode=campaign.planning_mode,
        plan_config=campaign.agent_touch_policy or {},
        touch_offsets=list(offsets),
        reason=(campaign.suggestion or {}).get("reason", ""),
    )


def waiting_for_cohort(session: Session, cohort: dict) -> list[int]:
    return waiting_client_ids(session, resolve_cohort_client_ids(session, **cohort))


def save_suggested_campaign(session: Session, draft: CampaignDraftIn, *, run_id: int) -> Campaign:
    campaign = create_campaign_record(
        session,
        name=draft.name,
        campaign_type="dormant_reengagement",
        cohort_filters=draft.cohort.model_dump(),
        planning_mode=draft.planning_mode,
        agent_touch_policy=draft.plan_config.model_dump(),
        proposed_by_run_id=run_id,
        suggestion=draft.model_dump(mode="json"),
        suggestion_state=SUGGESTION_PENDING,
    )
    _write_steps(session, campaign.campaign_id, draft.touch_offsets)
    record_audit(
        session,
        entity_type="campaign",
        action="suggest",
        entity_id=str(campaign.campaign_id),
        actor_id=AGENT_ACTOR,
        run_id=str(run_id),
        detail={"suggestion": draft.model_dump(mode="json")},
    )
    return campaign


def _pending(session: Session, campaign_id: int) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None or campaign.suggestion_state is None:
        raise SuggestionNotFound(campaign_id)
    if campaign.suggestion_state != SUGGESTION_PENDING:
        raise SuggestionNotPending(campaign.suggestion_state)
    return campaign


def edit_suggestion(
    session: Session, campaign_id: int, draft: CampaignDraftIn, *, edited_by: str
) -> Campaign:
    campaign = _pending(session, campaign_id)
    before = _shape(current_draft(session, campaign))
    campaign.name = draft.name
    campaign.cohort_definition = draft.cohort.model_dump()
    campaign.planning_mode = draft.planning_mode
    campaign.agent_touch_policy = draft.plan_config.model_dump()
    session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
    _write_steps(session, campaign_id, draft.touch_offsets)
    record_audit(
        session,
        entity_type="campaign",
        action="edit_suggestion",
        entity_id=str(campaign_id),
        actor_id=edited_by,
        detail={"from": before, "to": _shape(draft)},
    )
    return campaign


def _changes_from_suggestion(original: dict, current: CampaignDraftIn) -> dict:
    now = _shape(current)
    return {
        field: {"from": original.get(field), "to": now[field]}
        for field in SHAPE_FIELDS
        if original.get(field) != now[field]
    }


def approve_suggestion(
    session: Session, campaign_id: int, *, approved_by: str, permission: str
) -> int:
    campaign = _pending(session, campaign_id)
    require_campaign_ready(session, campaign)
    enrolled = enroll_campaign_cohort(session, campaign, waiting_only=True)
    if enrolled == 0:
        raise NothingWaiting(campaign_id)
    campaign.suggestion_state = SUGGESTION_APPROVED
    session.flush()
    record_audit(
        session,
        entity_type="campaign",
        action="approve_suggestion",
        entity_id=str(campaign_id),
        actor_id=approved_by,
        detail={
            "permission": permission,
            "enrolled_count": enrolled,
            "changes": _changes_from_suggestion(
                campaign.suggestion, current_draft(session, campaign)
            ),
        },
    )
    return enrolled


def dismiss_suggestion(
    session: Session, campaign_id: int, *, dismissed_by: str, reason: str
) -> None:
    campaign = _pending(session, campaign_id)
    campaign.suggestion_state = SUGGESTION_DISMISSED
    session.flush()
    record_audit(
        session,
        entity_type="campaign",
        action="dismiss_suggestion",
        entity_id=str(campaign_id),
        actor_id=dismissed_by,
        detail={"reason": reason},
    )


def suggested_campaign_out(session: Session, campaign: Campaign) -> SuggestedCampaignOut:
    return SuggestedCampaignOut(
        campaign_id=campaign.campaign_id,
        run_id=campaign.proposed_by_run_id,
        created_at=campaign.created_at,
        draft=current_draft(session, campaign),
        waiting_now=len(waiting_for_cohort(session, campaign.cohort_definition)),
        problems=campaign_readiness_problems(session, campaign),
    )


def list_pending_suggestions(session: Session) -> list[SuggestedCampaignOut]:
    campaigns = session.scalars(
        select(Campaign)
        .where(Campaign.suggestion_state == SUGGESTION_PENDING)
        .order_by(Campaign.campaign_id)
    ).all()
    return [suggested_campaign_out(session, campaign) for campaign in campaigns]


def live_agent_campaign_count(session: Session) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(Campaign)
            .where(
                Campaign.planning_mode == AGENT_PLANNING_MODE,
                Campaign.status.in_(LIVE_STATUSES),
                Campaign.suggestion_state.is_distinct_from(SUGGESTION_DISMISSED),
                Campaign.suggestion_state.is_distinct_from(SUGGESTION_PENDING),
            )
        )
        or 0
    )


def _held_back(session: Session, campaign: Campaign, reason: str) -> None:
    record_audit(
        session,
        entity_type="campaign",
        action="open_held_back",
        entity_id=str(campaign.campaign_id),
        actor_id=AGENT_ACTOR,
        detail={"reason": reason},
    )


def open_on_own(session: Session, campaign: Campaign) -> bool:
    if effective_permission(session, OPEN_CAMPAIGN_ACTION) != ACT_ALONE:
        return False
    limit = get_settings().agent_campaign_max_live
    if live_agent_campaign_count(session) >= limit:
        _held_back(session, campaign, f"{limit} agent campaigns are already live")
        return False
    try:
        approve_suggestion(
            session, campaign.campaign_id, approved_by=AGENT_ACTOR, permission=ACT_ALONE
        )
    except CampaignNotReady as exc:
        _held_back(session, campaign, str(exc))
        return False
    except NothingWaiting:
        _held_back(session, campaign, "nobody in this group is waiting any more")
        return False
    return True
