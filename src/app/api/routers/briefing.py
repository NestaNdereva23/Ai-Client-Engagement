from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.api.reviewer_auth import get_current_reviewer_id
from app.config import get_settings
from app.db.session import get_session
from app.privacy.llm_client import get_briefing_llm_client
from app.schemas.briefing import BriefingOut, ClientEmailDraftOut
from app.services.briefing import (
    BriefingNotFound,
    NarrativeDisabled,
    get_briefing,
    get_narrative_briefing,
)
from app.services.client_email_draft import (
    DailyDraftLimitReached,
    EmailDraftBlocked,
    EmailDraftDisabled,
    EmailDraftFailed,
    draft_email_for_client,
)

router = APIRouter(
    prefix="/briefing", tags=["briefing"], dependencies=[Depends(get_current_reviewer_id)]
)


def _narrative_disabled() -> ApiError:
    return ApiError(
        status_code=404,
        code="narrative_disabled",
        detail="AI-narrated briefings are not enabled",
    )


def _email_draft_disabled() -> ApiError:
    return ApiError(
        status_code=404,
        code="email_draft_disabled",
        detail="AI email drafts are not switched on",
    )


@router.get("/{client_id}/{unit_fund_id}", response_model=BriefingOut)
def get_client_briefing(
    client_id: int,
    unit_fund_id: int,
    username: str = Query(
        ..., description="The viewing user's identifier, recorded on the audit row."
    ),
    session: Session = Depends(get_session),
) -> BriefingOut:
    try:
        view = get_briefing(session, client_id, unit_fund_id, viewing_fa_id=username)
    except BriefingNotFound:
        raise HTTPException(
            status_code=404, detail="no briefing available for this client"
        ) from None
    return BriefingOut.model_validate(view)


@router.get("/{client_id}/{unit_fund_id}/narrative", response_model=BriefingOut)
def get_client_briefing_narrative(
    client_id: int,
    unit_fund_id: int,
    username: str = Query(
        ..., description="The viewing user's identifier, recorded on the audit row."
    ),
    session: Session = Depends(get_session),
) -> BriefingOut:
    settings = get_settings()
    if not settings.ai_briefing_enabled:
        raise _narrative_disabled()
    try:
        view = get_narrative_briefing(
            session,
            client_id,
            unit_fund_id,
            viewing_fa_id=username,
            llm_client=get_briefing_llm_client(settings),
        )
    except NarrativeDisabled:
        raise _narrative_disabled() from None
    except BriefingNotFound:
        raise HTTPException(
            status_code=404, detail="no briefing available for this client"
        ) from None
    return BriefingOut.model_validate(view)


@router.post("/{client_id}/{unit_fund_id}/email-draft", response_model=ClientEmailDraftOut)
def post_client_email_draft(
    client_id: int,
    unit_fund_id: int,
    username: str = Query(..., description="The advisor asking. Counted toward the daily cap."),
    session: Session = Depends(get_session),
) -> ClientEmailDraftOut:
    settings = get_settings()
    if not settings.ai_briefing_enabled:
        raise _email_draft_disabled()
    try:
        draft = draft_email_for_client(
            session,
            client_id,
            unit_fund_id,
            username=username,
            llm_client=get_briefing_llm_client(settings),
        )
    except EmailDraftDisabled:
        raise _email_draft_disabled() from None
    except BriefingNotFound:
        raise HTTPException(status_code=404, detail="active client-fund not found") from None
    except EmailDraftBlocked:
        raise ApiError(
            status_code=409,
            code="email_draft_blocked",
            detail="This client has an open complaint or asked not to be contacted",
        ) from None
    except DailyDraftLimitReached:
        raise ApiError(
            status_code=429,
            code="email_draft_limit_reached",
            detail="You have used today's email drafts",
        ) from None
    except EmailDraftFailed:
        raise ApiError(
            status_code=502,
            code="email_draft_failed",
            detail="The draft could not be made. Try again",
        ) from None
    return ClientEmailDraftOut(
        message_id=draft.message_id, drafts_left_today=draft.drafts_left_today
    )
