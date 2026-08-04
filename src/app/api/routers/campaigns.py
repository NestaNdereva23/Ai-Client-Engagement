"""Campaign console: enrollment summary, including primary-row suppression."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.schemas.campaigns import CampaignSummaryOut
from app.services.campaigns import CampaignNotFound, campaign_summary

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.get("/{campaign_id}/summary", response_model=CampaignSummaryOut)
def get_campaign_summary(
    campaign_id: int, session: Session = Depends(get_session)
) -> CampaignSummaryOut:
    """Enrollment counts for one campaign, including rows suppressed as a duplicate person."""
    try:
        summary = campaign_summary(session, campaign_id)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return CampaignSummaryOut(campaign_id=campaign_id, **summary)
