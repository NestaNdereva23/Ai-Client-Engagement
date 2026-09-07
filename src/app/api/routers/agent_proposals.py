from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agents.proposal_state import InvalidTransition
from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.agent_proposals import (
    AgentProposalClientOut,
    AgentProposalDetailOut,
    AgentProposalSummaryOut,
    ProposalDecisionRequest,
    ProposalDecisionResultOut,
)
from app.services.agent_proposals import (
    ProposalNotFound,
    count_proposals,
    decide_proposal,
    get_proposal,
    get_proposal_clients,
    list_proposals,
)

router = APIRouter(
    prefix="/agent/proposals",
    tags=["agent_proposals"],
    dependencies=[Depends(get_current_reviewer_id)],
)


@router.get("", response_model=Page[AgentProposalSummaryOut])
def list_agent_proposals(
    status: str | None = None,
    proposal_date: date | None = Query(default=None, alias="date"),
    action_code: str | None = None,
    exclude_action_code: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[AgentProposalSummaryOut]:
    try:
        proposals, next_cursor = list_proposals(
            session,
            status=status,
            proposal_date=proposal_date,
            action_code=action_code,
            exclude_action_code=exclude_action_code,
            cursor=cursor,
            limit=limit,
        )
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    total_count = count_proposals(
        session,
        status=status,
        proposal_date=proposal_date,
        action_code=action_code,
        exclude_action_code=exclude_action_code,
    )
    return Page(
        items=[
            AgentProposalSummaryOut(
                proposal_id=p.proposal_id,
                action_code=p.action_code,
                group_name=p.group_name,
                client_count=p.client_count,
                included_count=included_count,
                money_total_kes=p.money_total_kes,
                status=p.status,
                permission_applied=p.permission_applied,
                created_at=p.created_at,
                decided_at=p.decided_at,
            )
            for p, included_count in proposals
        ],
        next_cursor=next_cursor,
        total_count=total_count,
    )


@router.get("/{proposal_id}", response_model=AgentProposalDetailOut)
def get_agent_proposal(
    proposal_id: int, session: Session = Depends(get_session)
) -> AgentProposalDetailOut:
    try:
        proposal = get_proposal(session, proposal_id)
    except ProposalNotFound:
        raise HTTPException(status_code=404, detail="proposal not found") from None

    clients = get_proposal_clients(session, proposal_id)
    included_count = sum(1 for c in clients if c.included) if clients else None
    return AgentProposalDetailOut(
        proposal_id=proposal.proposal_id,
        action_code=proposal.action_code,
        group_name=proposal.group_name,
        client_count=proposal.client_count,
        included_count=included_count,
        money_total_kes=proposal.money_total_kes,
        status=proposal.status,
        permission_applied=proposal.permission_applied,
        created_at=proposal.created_at,
        decided_at=proposal.decided_at,
        group_definition=proposal.group_definition,
        evidence=proposal.evidence,
        reason=proposal.reason,
        angle=proposal.angle,
        content_mix=proposal.content_mix,
        campaign_id=proposal.campaign_id,
        decided_by=proposal.decided_by,
        clients=[AgentProposalClientOut.model_validate(c) for c in clients],
    )


@router.post("/{proposal_id}/decision", response_model=ProposalDecisionResultOut)
def decide_agent_proposal(
    proposal_id: int,
    body: ProposalDecisionRequest,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> ProposalDecisionResultOut:
    try:
        proposal = decide_proposal(
            session,
            proposal_id,
            decision=body.decision,
            reason=body.reason,
            decided_by=reviewer_id,
        )
        session.commit()
    except ProposalNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="proposal not found") from None
    except InvalidTransition as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None

    return ProposalDecisionResultOut.model_validate(proposal)
