from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.templates import (
    DecideBatchTemplateFailureOut,
    DecideBatchTemplateRequest,
    DecideBatchTemplateResultOut,
    DecideTemplateRequest,
    MessageTemplateDetail,
    MessageTemplateSummary,
    TemplateReviewActionOut,
)
from app.services.template_review import (
    EditedContentRequired,
    InvalidOutcome,
    TemplateAlreadyDecided,
    decide_template_batch,
    get_template,
    get_template_review_history,
    list_pending_templates,
)
from app.services.template_review import TemplateNotFound as TemplateNotFoundError
from app.services.template_review import decide_template as decide_template_action

router = APIRouter(
    prefix="/templates", tags=["templates"], dependencies=[Depends(get_current_reviewer_id)]
)


@router.get("", response_model=Page[MessageTemplateSummary])
def list_templates(
    status: str = "pending_review",
    campaign_id: int | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[MessageTemplateSummary]:
    try:
        templates, next_cursor = list_pending_templates(
            session, status=status, campaign_id=campaign_id, cursor=cursor, limit=limit
        )
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(
        items=[MessageTemplateSummary.model_validate(t) for t in templates],
        next_cursor=next_cursor,
    )


@router.get("/{template_id}", response_model=MessageTemplateDetail)
def get_template_detail(
    template_id: str, session: Session = Depends(get_session)
) -> MessageTemplateDetail:
    try:
        template = get_template(session, template_id)
    except TemplateNotFoundError:
        raise HTTPException(status_code=404, detail="template not found") from None

    history = get_template_review_history(session, template_id)
    return MessageTemplateDetail(
        template_id=template.template_id,
        campaign_id=template.campaign_id,
        generation_run_id=template.generation_run_id,
        status=template.status,
        profile_key=template.profile_key,
        ai_draft_content=template.ai_draft_content,
        created_at=template.created_at,
        updated_at=template.updated_at,
        history=[TemplateReviewActionOut.model_validate(a) for a in history],
    )


@router.post("/{template_id}/decide", response_model=TemplateReviewActionOut)
def decide_review(
    template_id: str,
    body: DecideTemplateRequest,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> TemplateReviewActionOut:
    try:
        action = decide_template_action(
            session,
            template_id,
            outcome=body.outcome,
            reviewer_id=reviewer_id,
            reason=body.reason,
            edited_content=body.edited_content,
        )
        session.commit()
    except TemplateNotFoundError:
        session.rollback()
        raise HTTPException(status_code=404, detail="template not found") from None
    except TemplateAlreadyDecided as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (EditedContentRequired, InvalidOutcome) as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None

    return TemplateReviewActionOut.model_validate(action)


@router.post("/decide-batch", response_model=DecideBatchTemplateResultOut)
def decide_templates_batch(
    body: DecideBatchTemplateRequest,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> DecideBatchTemplateResultOut:
    try:
        result = decide_template_batch(
            session,
            body.template_ids,
            outcome=body.outcome,
            reviewer_id=reviewer_id,
            reason=body.reason,
        )
        session.commit()
    except (EditedContentRequired, InvalidOutcome) as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None

    return DecideBatchTemplateResultOut(
        decided=[TemplateReviewActionOut.model_validate(a) for a in result.decided],
        failed=[DecideBatchTemplateFailureOut.model_validate(f) for f in result.failed],
    )
