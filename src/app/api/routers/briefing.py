from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.api.reviewer_auth import get_current_reviewer_id
from app.config import get_settings
from app.db.session import get_session
from app.privacy.llm_client import get_briefing_llm_client
from app.schemas.briefing import BriefingOut
from app.services.briefing import (
    BriefingNotFound,
    NarrativeDisabled,
    get_briefing,
    get_narrative_briefing,
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
