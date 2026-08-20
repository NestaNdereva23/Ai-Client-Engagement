"""Briefing endpoints: one client's deterministic risk briefing, and its
optional AI-narrated counterpart (AM15).

Re-attaches a real name (see briefing/render.py and services/briefing.py),
same as GET /clients/{id}/name, but deliberately isn't behind the
reviewer-key gate that endpoint uses: an FA re-entering a credential for
every row of a morning brief was worse friction than the gate was worth for
a read that's still fully attributed and audited, just by fa_id rather than
a reviewer key. fa_id is a required query param precisely so every read
still names who looked, even with no key involved. The name-reveal endpoint
keeps the X-Reviewer-Key gate -- this is the one deliberate exception, not
a precedent for loosening it elsewhere.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.errors import ApiError
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

router = APIRouter(prefix="/briefing", tags=["briefing"])


def _narrative_disabled() -> ApiError:
    """The one 404 that means the feature is off rather than the data missing."""
    return ApiError(
        status_code=404,
        code="narrative_disabled",
        detail="AI-narrated briefings are not enabled",
    )


@router.get("/{client_id}/{unit_fund_id}", response_model=BriefingOut)
def get_client_briefing(
    client_id: int,
    unit_fund_id: int,
    fa_id: str = Query(..., description="The viewing FA's identifier, recorded on the audit row."),
    session: Session = Depends(get_session),
) -> BriefingOut:
    """The plain-text briefing page for one client-fund relationship.

    404s when there is no client_risk_features or active_client_fund row
    for this key -- there is not enough data to render a page that means
    anything.
    """
    try:
        view = get_briefing(session, client_id, unit_fund_id, viewing_fa_id=fa_id)
    except BriefingNotFound:
        raise HTTPException(
            status_code=404, detail="no briefing available for this client"
        ) from None
    return BriefingOut.model_validate(view)


@router.get("/{client_id}/{unit_fund_id}/narrative", response_model=BriefingOut)
def get_client_briefing_narrative(
    client_id: int,
    unit_fund_id: int,
    fa_id: str = Query(..., description="The viewing FA's identifier, recorded on the audit row."),
    session: Session = Depends(get_session),
) -> BriefingOut:
    """The optional, model-narrated version of the same briefing page.

    404s when the feature is off (settings.ai_briefing_enabled), and on the
    same missing-data case the deterministic route 404s on. Those two carry
    different error codes, narrative_disabled and not_found, so a caller can
    tell "this environment does not narrate" from "we have no data on this
    client" without reading the message text. Never fails on a model or
    grounding problem -- the response's mode field says whether it actually
    got a narrative or fell back to the deterministic text.
    """
    settings = get_settings()
    # Checked here too, before building a model client for nothing, even
    # though get_narrative_briefing checks the same flag itself -- see
    # NarrativeDisabled below, kept as the defense-in-depth backstop for any
    # other caller of that function.
    if not settings.ai_briefing_enabled:
        raise _narrative_disabled()
    try:
        view = get_narrative_briefing(
            session,
            client_id,
            unit_fund_id,
            viewing_fa_id=fa_id,
            llm_client=get_briefing_llm_client(settings),
        )
    except NarrativeDisabled:
        raise _narrative_disabled() from None
    except BriefingNotFound:
        raise HTTPException(
            status_code=404, detail="no briefing available for this client"
        ) from None
    return BriefingOut.model_validate(view)
