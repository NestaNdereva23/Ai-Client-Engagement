from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.agent_scope import get_agent_scope
from app.api.reviewer_auth import bearer_scheme
from app.db.models.outreach import AGENT_PROPOSAL_CAMPAIGN_TYPE, Campaign
from app.db.session import get_session
from app.services.agent_campaign_scope import campaign_is_visible

# Only the active agent's proposal campaigns belong to an advisor's book. For every
# other campaign nothing here reads the advisor scope, its setting or view as.


def require_campaign_in_scope(
    campaign_id: int,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_session),
) -> None:
    campaign_type = session.scalar(
        select(Campaign.campaign_type).where(Campaign.campaign_id == campaign_id)
    )
    if campaign_type != AGENT_PROPOSAL_CAMPAIGN_TYPE:
        return
    scope = get_agent_scope(credentials, session)
    if not scope.is_everyone and not campaign_is_visible(session, campaign_id, scope.fa_id):
        raise HTTPException(status_code=404, detail="campaign not found")


def listing_fa_id(
    campaign_type: str | None = None,
    exclude_campaign_type: str | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_session),
) -> str | None:
    if campaign_type not in (None, AGENT_PROPOSAL_CAMPAIGN_TYPE):
        return None
    if exclude_campaign_type == AGENT_PROPOSAL_CAMPAIGN_TYPE:
        return None
    return get_agent_scope(credentials, session).fa_id
