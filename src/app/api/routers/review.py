from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agents.email_channel import build_default_agent
from app.api.reviewer_auth import get_current_reviewer_id
from app.campaigns.generation import (
    MessageNotRegenerable,
    RegenerationRejected,
    model_boundary_audit_sink,
    regenerate_message,
)
from app.config import get_settings
from app.db.session import get_session
from app.llmops.tracing import get_shared_tracer
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.review import (
    CohortReadyOut,
    DecideBatchFailureOut,
    DecideBatchRequest,
    DecideBatchResultOut,
    DecideRequest,
    DecideResultOut,
    OutreachMessageDetail,
    OutreachMessageSummary,
    ReviewActionOut,
    ReviewOrder,
)
from app.services.review import (
    CohortNotFound,
    CohortNotReady,
    EditedContentRequired,
    InvalidOutcome,
    MessageAlreadyDecided,
    MessageNotFound,
    check_cohort_ready,
    count_pending_messages,
    get_message,
    get_review_history,
    list_pending_messages,
)
from app.services.review import approve_cohort_remainder as approve_cohort_remainder_service
from app.services.review import decide as decide_message
from app.services.review import decide_batch as decide_message_batch

router = APIRouter(
    prefix="/reviews", tags=["review"], dependencies=[Depends(get_current_reviewer_id)]
)


@router.get("", response_model=Page[OutreachMessageSummary])
def list_reviews(
    status: str = "pending_review",
    campaign_id: int | None = None,
    only_sampled: bool = True,
    order: ReviewOrder = "oldest_first",
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[OutreachMessageSummary]:
    try:
        messages, next_cursor = list_pending_messages(
            session,
            status=status,
            campaign_id=campaign_id,
            only_sampled=only_sampled,
            order=order,
            cursor=cursor,
            limit=limit,
        )
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    total_count = count_pending_messages(
        session, status=status, campaign_id=campaign_id, only_sampled=only_sampled
    )
    return Page(
        items=[OutreachMessageSummary.model_validate(m) for m in messages],
        next_cursor=next_cursor,
        total_count=total_count,
    )


@router.get("/{message_id}", response_model=OutreachMessageDetail)
def get_review(message_id: str, session: Session = Depends(get_session)) -> OutreachMessageDetail:
    try:
        message = get_message(session, message_id)
    except MessageNotFound:
        raise HTTPException(status_code=404, detail="message not found") from None

    history = get_review_history(session, message_id)
    return OutreachMessageDetail(
        message_id=message.message_id,
        campaign_id=message.campaign_id,
        client_id=message.client_id,
        channel=message.channel,
        status=message.status,
        created_at=message.created_at,
        ai_draft_content=message.ai_draft_content,
        personalized_content=message.personalized_content,
        call_brief=message.call_brief,
        cohort_id=message.cohort_id,
        is_sample=message.is_sample,
        updated_at=message.updated_at,
        history=[ReviewActionOut.model_validate(a) for a in history],
    )


@router.post("/{message_id}/decide", response_model=DecideResultOut)
def decide_review(
    message_id: str,
    body: DecideRequest,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> DecideResultOut:
    try:
        message = get_message(session, message_id)
        action = decide_message(
            session,
            message_id,
            outcome=body.outcome,
            reviewer_id=reviewer_id,
            reason=body.reason,
            edited_content=body.edited_content,
        )
        cohort_ready = check_cohort_ready(session, message)
        session.commit()
    except MessageNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="message not found") from None
    except MessageAlreadyDecided as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (EditedContentRequired, InvalidOutcome) as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None

    return DecideResultOut(
        action=ReviewActionOut.model_validate(action),
        cohort_ready=CohortReadyOut.model_validate(cohort_ready) if cohort_ready else None,
    )


@router.post("/cohorts/{cohort_id}/approve-remaining", response_model=DecideBatchResultOut)
def approve_cohort_remaining(
    cohort_id: str,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> DecideBatchResultOut:
    try:
        result = approve_cohort_remainder_service(session, cohort_id, reviewer_id=reviewer_id)
        session.commit()
    except CohortNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="cohort not found") from None
    except CohortNotReady as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail=f"cohort is not ready to approve the rest ({exc.status})"
        ) from None

    return DecideBatchResultOut(
        decided=[ReviewActionOut.model_validate(a) for a in result.decided],
        failed=[DecideBatchFailureOut.model_validate(f) for f in result.failed],
    )


@router.post("/decide-batch", response_model=DecideBatchResultOut)
def decide_reviews_batch(
    body: DecideBatchRequest,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> DecideBatchResultOut:
    try:
        result = decide_message_batch(
            session,
            body.message_ids,
            outcome=body.outcome,
            reviewer_id=reviewer_id,
            reason=body.reason,
        )
        session.commit()
    except (EditedContentRequired, InvalidOutcome) as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None

    return DecideBatchResultOut(
        decided=[ReviewActionOut.model_validate(a) for a in result.decided],
        failed=[DecideBatchFailureOut.model_validate(f) for f in result.failed],
    )


@router.post("/{message_id}/regenerate", response_model=OutreachMessageDetail)
def regenerate_review(
    message_id: str, session: Session = Depends(get_session)
) -> OutreachMessageDetail:
    tracer = get_shared_tracer()
    agent = build_default_agent(session, audit=model_boundary_audit_sink(session), tracer=tracer)
    try:
        fresh = regenerate_message(
            session, message_id, agent=agent, settings=get_settings(), tracer=tracer
        )
        session.commit()
    except MessageNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="message not found") from None
    except MessageNotRegenerable as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail=f"message is already {exc.status}; cannot regenerate"
        ) from None
    except RegenerationRejected:
        session.rollback()
        raise HTTPException(
            status_code=422,
            detail=(
                "the fresh draft was rejected by every guardrail retry; "
                "the original message is unchanged"
            ),
        ) from None

    return OutreachMessageDetail(
        message_id=fresh.message_id,
        campaign_id=fresh.campaign_id,
        client_id=fresh.client_id,
        channel=fresh.channel,
        status=fresh.status,
        created_at=fresh.created_at,
        ai_draft_content=fresh.ai_draft_content,
        personalized_content=fresh.personalized_content,
        call_brief=fresh.call_brief,
        cohort_id=fresh.cohort_id,
        is_sample=fresh.is_sample,
        updated_at=fresh.updated_at,
        history=[],
    )
