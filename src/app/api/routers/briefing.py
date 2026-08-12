"""Briefing endpoints: one client's deterministic risk briefing.

Re-attaches a real name (see briefing/render.py and services/briefing.py),
so this sits behind the same X-Reviewer-Key stopgap
GET /clients/{id}/name uses -- fails closed with no key configured, and
every successful read is audited.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.reviewer_auth import require_reviewer_key
from app.db.session import get_session
from app.schemas.briefing import BriefingOut
from app.services.briefing import BriefingNotFound, get_briefing

router = APIRouter(prefix="/briefing", tags=["briefing"])


@router.get(
    "/{client_id}/{unit_fund_id}",
    response_model=BriefingOut,
    dependencies=[Depends(require_reviewer_key)],
)
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
