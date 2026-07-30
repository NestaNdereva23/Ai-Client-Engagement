"""Review queue: list pending messages, open one, and decide."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.review import (
    DecideRequest,
    OutreachMessageDetail,
    OutreachMessageSummary,
    ReviewActionOut,
)
from app.services.review import (
    EditedContentRequired,
    InvalidOutcome,
    MessageAlreadyDecided,
    MessageNotFound,
    get_message,
    get_review_history,
    list_pending_messages,
)
from app.services.review import decide as decide_message

router = APIRouter(prefix="/reviews", tags=["review"])


@router.get("", response_model=Page[OutreachMessageSummary])
def list_reviews(
    status: str = "pending_review",
    campaign_id: int | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[OutreachMessageSummary]:
    """The reviewer's queue: one page of messages in the given status, oldest first."""
    try:
        messages, next_cursor = list_pending_messages(
            session, status=status, campaign_id=campaign_id, cursor=cursor, limit=limit
        )
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(
        items=[OutreachMessageSummary.model_validate(m) for m in messages], next_cursor=next_cursor
    )


@router.get("/{message_id}", response_model=OutreachMessageDetail)
def get_review(message_id: str, session: Session = Depends(get_session)) -> OutreachMessageDetail:
    """One message: both content versions and its full decision history."""
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
        updated_at=message.updated_at,
        history=[ReviewActionOut.model_validate(a) for a in history],
    )


@router.post("/{message_id}/decide", response_model=ReviewActionOut)
def decide_review(
    message_id: str, body: DecideRequest, session: Session = Depends(get_session)
) -> ReviewActionOut:
    """Approve, edit-approve, reject, escalate, or hold one message."""
    try:
        action = decide_message(
            session,
            message_id,
            outcome=body.outcome,
            reviewer_id=body.reviewer_id,
            reason=body.reason,
            edited_content=body.edited_content,
        )
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

    return ReviewActionOut.model_validate(action)
