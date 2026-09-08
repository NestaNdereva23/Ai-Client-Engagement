from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agents.insight_state import InvalidTransition
from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.agent_insights import (
    AgentInsightDetailOut,
    AgentInsightFactOut,
    AgentInsightSummaryOut,
    FactRecountOut,
    InsightDecisionRequest,
    InsightDecisionResultOut,
    InsightKindCountsOut,
)
from app.services.agent_insights import (
    FactNotFound,
    InsightNotFound,
    count_insight_clients,
    count_insights,
    count_insights_by_kind,
    decide_insight,
    get_insight,
    get_insight_facts,
    list_insights,
    recheck_insight_fact,
)

router = APIRouter(
    prefix="/agent/insights",
    tags=["agent_insights"],
    dependencies=[Depends(get_current_reviewer_id)],
)


@router.get("", response_model=Page[AgentInsightSummaryOut])
def list_agent_insights(
    run_id: int | None = None,
    insight_date: date | None = Query(default=None, alias="date"),
    kind: str | None = None,
    state: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[AgentInsightSummaryOut]:
    try:
        insights, next_cursor = list_insights(
            session,
            run_id=run_id,
            insight_date=insight_date,
            kind=kind,
            state=state,
            cursor=cursor,
            limit=limit,
        )
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None

    total_count = count_insights(
        session, run_id=run_id, insight_date=insight_date, kind=kind, state=state
    )
    return Page(
        items=[AgentInsightSummaryOut.model_validate(row) for row in insights],
        next_cursor=next_cursor,
        total_count=total_count,
    )


@router.get("/counts", response_model=InsightKindCountsOut)
def count_agent_insights_by_kind(
    run_id: int | None = None,
    insight_date: date | None = Query(default=None, alias="date"),
    state: str | None = None,
    session: Session = Depends(get_session),
) -> InsightKindCountsOut:
    counts = count_insights_by_kind(session, run_id=run_id, insight_date=insight_date, state=state)
    return InsightKindCountsOut(counts_by_kind=counts, total_count=sum(counts.values()))


@router.get("/{insight_id}", response_model=AgentInsightDetailOut)
def get_agent_insight(
    insight_id: int, session: Session = Depends(get_session)
) -> AgentInsightDetailOut:
    try:
        insight = get_insight(session, insight_id)
    except InsightNotFound:
        raise HTTPException(status_code=404, detail="insight not found") from None

    facts = get_insight_facts(session, insight_id)
    return AgentInsightDetailOut(
        insight_id=insight.insight_id,
        run_id=insight.run_id,
        kind=insight.kind,
        title=insight.title,
        group_name=insight.group_name,
        client_count=insight.client_count,
        money_total_kes=insight.money_total_kes,
        confidence=insight.confidence,
        suggestion=insight.suggestion,
        state=insight.state,
        created_at=insight.created_at,
        group_definition=insight.group_definition,
        confidence_reason=insight.confidence_reason,
        avoid_saying=insight.avoid_saying,
        why_now=insight.why_now,
        dismissed_reason=insight.dismissed_reason,
        decided_by=insight.decided_by,
        decided_at=insight.decided_at,
        listed_client_count=count_insight_clients(session, insight_id),
        facts=[AgentInsightFactOut.model_validate(fact) for fact in facts],
    )


@router.post("/{insight_id}/decision", response_model=InsightDecisionResultOut)
def decide_agent_insight(
    insight_id: int,
    body: InsightDecisionRequest,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> InsightDecisionResultOut:
    try:
        insight = decide_insight(
            session,
            insight_id,
            decision=body.decision,
            reason=body.reason,
            decided_by=reviewer_id,
        )
        session.commit()
    except InsightNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="insight not found") from None
    except InvalidTransition as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None

    return InsightDecisionResultOut.model_validate(insight)


@router.get("/{insight_id}/facts/{fact_id}/recount", response_model=FactRecountOut)
def recount_agent_insight_fact(
    insight_id: int,
    fact_id: int,
    as_of: date | None = None,
    session: Session = Depends(get_session),
) -> FactRecountOut:
    try:
        result = recheck_insight_fact(session, insight_id, fact_id, as_of=as_of or date.today())
    except InsightNotFound:
        raise HTTPException(status_code=404, detail="insight not found") from None
    except FactNotFound:
        raise HTTPException(status_code=404, detail="fact not found") from None

    return FactRecountOut(
        fact_id=fact_id,
        as_of=result.as_of,
        stored_value=result.stored_value,
        can_recount=result.can_recount,
        group_name=result.group_name,
        client_count=result.client_count,
        fund_count=result.fund_count,
        money_total_kes=result.money_total_kes,
        reason=result.reason,
    )
