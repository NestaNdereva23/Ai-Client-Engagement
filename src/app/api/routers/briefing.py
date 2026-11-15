from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.access import Access, get_access, require_client_in_book
from app.api.errors import ApiError
from app.config import get_settings
from app.db.session import get_session
from app.llmops.tracing import get_shared_tracer
from app.privacy.llm_client import get_briefing_llm_client
from app.schemas.briefing import BriefingOut, ClientEmailDraftOut
from app.schemas.client_assistant import EmailDraftIn
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
from app.services.email_options import OptionNotFound, find_chosen_option

router = APIRouter(prefix="/briefing", tags=["briefing"], dependencies=[Depends(get_access)])


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
    access: Access = Depends(require_client_in_book),
    session: Session = Depends(get_session),
) -> BriefingOut:
    try:
        view = get_briefing(
            session,
            client_id,
            unit_fund_id,
            viewer_id=access.email,
            on_behalf_of=access.on_behalf_of,
        )
    except BriefingNotFound:
        raise HTTPException(
            status_code=404, detail="no briefing available for this client"
        ) from None
    return BriefingOut.model_validate(view)


@router.get("/{client_id}/{unit_fund_id}/narrative", response_model=BriefingOut)
def get_client_briefing_narrative(
    client_id: int,
    unit_fund_id: int,
    access: Access = Depends(require_client_in_book),
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
            viewer_id=access.email,
            on_behalf_of=access.on_behalf_of,
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
    body: EmailDraftIn | None = None,
    access: Access = Depends(require_client_in_book),
    session: Session = Depends(get_session),
) -> ClientEmailDraftOut:
    settings = get_settings()
    if not settings.ai_briefing_enabled:
        raise _email_draft_disabled()
    angle = None
    if body is not None:
        try:
            angle = find_chosen_option(
                session,
                client_id=client_id,
                unit_fund_id=unit_fund_id,
                turn_id=body.turn_id,
                option_id=body.option_id,
                username=access.email,
                sees_every_book=access.sees_every_book,
            ).angle
        except OptionNotFound:
            raise HTTPException(status_code=404, detail="email option not found") from None
    try:
        draft = draft_email_for_client(
            session,
            client_id,
            unit_fund_id,
            username=access.email,
            on_behalf_of=access.on_behalf_of,
            llm_client=get_briefing_llm_client(settings),
            angle=angle,
            tracer=get_shared_tracer(),
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
