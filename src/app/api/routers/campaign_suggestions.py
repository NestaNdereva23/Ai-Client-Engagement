from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.agents.campaign_suggester import (
    SuggestionRunInProgress,
    run_suggestions_in_background,
    start_suggestion_run,
)
from app.agents.permissions import effective_permission
from app.api.access import get_admin_access
from app.api.reviewer_auth import get_current_reviewer_id
from app.campaigns.readiness import CampaignNotReady
from app.campaigns.waiting_pool import waiting_pool_summary
from app.db.session import get_session
from app.schemas.agent_runs import AgentRunOut
from app.schemas.campaigns import (
    CampaignDraftIn,
    SuggestedCampaignOut,
    SuggestionApprovedOut,
    SuggestionDismissIn,
    WaitingPoolOut,
)
from app.services.campaign_suggestions import (
    OPEN_CAMPAIGN_ACTION,
    NothingWaiting,
    SuggestionNotFound,
    SuggestionNotPending,
    approve_suggestion,
    dismiss_suggestion,
    edit_suggestion,
    list_pending_suggestions,
    suggested_campaign_out,
)

router = APIRouter(
    prefix="/campaigns/suggestions",
    tags=["campaign_suggestions"],
    dependencies=[Depends(get_current_reviewer_id), Depends(get_admin_access)],
)


@router.post("/run", response_model=AgentRunOut, status_code=202)
def post_suggestion_run(
    background_tasks: BackgroundTasks, session: Session = Depends(get_session)
) -> AgentRunOut:
    try:
        run = start_suggestion_run(session)
    except SuggestionRunInProgress as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    background_tasks.add_task(run_suggestions_in_background, run.run_id)
    return AgentRunOut.model_validate(run)


@router.get("", response_model=list[SuggestedCampaignOut])
def get_suggestions(session: Session = Depends(get_session)) -> list[SuggestedCampaignOut]:
    return list_pending_suggestions(session)


@router.get("/waiting-pool", response_model=WaitingPoolOut)
def get_waiting_pool(session: Session = Depends(get_session)) -> WaitingPoolOut:
    return WaitingPoolOut(**waiting_pool_summary(session))


@router.put("/{campaign_id}", response_model=SuggestedCampaignOut)
def put_suggestion(
    campaign_id: int,
    body: CampaignDraftIn,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> SuggestedCampaignOut:
    try:
        campaign = edit_suggestion(session, campaign_id, body, edited_by=reviewer_id)
        session.commit()
    except SuggestionNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="suggestion not found") from None
    except SuggestionNotPending:
        session.rollback()
        raise HTTPException(status_code=409, detail="suggestion was already decided") from None
    return suggested_campaign_out(session, campaign)


@router.post("/{campaign_id}/approve", response_model=SuggestionApprovedOut)
def post_suggestion_approve(
    campaign_id: int,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> SuggestionApprovedOut:
    try:
        enrolled = approve_suggestion(
            session,
            campaign_id,
            approved_by=reviewer_id,
            permission=effective_permission(session, OPEN_CAMPAIGN_ACTION),
        )
        session.commit()
    except SuggestionNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="suggestion not found") from None
    except SuggestionNotPending:
        session.rollback()
        raise HTTPException(status_code=409, detail="suggestion was already decided") from None
    except (CampaignNotReady, NothingWaiting) as exc:
        session.rollback()
        detail = (
            str(exc) if isinstance(exc, CampaignNotReady) else "nobody in this group is waiting"
        )
        raise HTTPException(status_code=409, detail=detail) from None
    return SuggestionApprovedOut(campaign_id=campaign_id, enrolled_count=enrolled)


@router.post("/{campaign_id}/dismiss", status_code=204)
def post_suggestion_dismiss(
    campaign_id: int,
    body: SuggestionDismissIn,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> Response:
    try:
        dismiss_suggestion(session, campaign_id, dismissed_by=reviewer_id, reason=body.reason)
        session.commit()
    except SuggestionNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="suggestion not found") from None
    except SuggestionNotPending:
        session.rollback()
        raise HTTPException(status_code=409, detail="suggestion was already decided") from None
    return Response(status_code=204)
