"""Campaign console: list, create, and enrollment summary."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.campaigns import (
    CampaignCreateOut,
    CampaignCreateRequest,
    CampaignListItemOut,
    CampaignSummaryOut,
)
from app.services.campaigns import (
    CampaignNotFound,
    campaign_summary,
    create_campaign,
    list_campaigns,
)

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.get("", response_model=Page[CampaignListItemOut])
def get_campaigns(
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[CampaignListItemOut]:
    """Campaigns oldest-first, each carrying its own enrollment counts."""
    try:
        rows, next_cursor = list_campaigns(session, status=status, cursor=cursor, limit=limit)
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    items = [
        CampaignListItemOut(
            campaign_id=r.campaign_id,
            name=r.name,
            campaign_type=r.campaign_type,
            status=r.status,
            cohort_definition=r.cohort_definition,
            start_date=r.start_date,
            end_date=r.end_date,
            created_at=r.created_at,
            total_enrolled=r.total_enrolled,
            primary_count=r.primary_count,
            suppressed_count=r.total_enrolled - r.primary_count,
        )
        for r in rows
    ]
    return Page(items=items, next_cursor=next_cursor)


@router.post("", response_model=CampaignCreateOut, status_code=201)
def post_campaign(
    body: CampaignCreateRequest, session: Session = Depends(get_session)
) -> CampaignCreateOut:
    """Create a campaign and enroll every client currently matching its cohort filter."""
    campaign, enrolled_count = create_campaign(
        session,
        name=body.name,
        campaign_type=body.campaign_type,
        cohort_filters=body.cohort.model_dump(),
        start_date=body.start_date,
        end_date=body.end_date,
    )
    session.commit()
    return CampaignCreateOut(
        campaign_id=campaign.campaign_id,
        name=campaign.name,
        campaign_type=campaign.campaign_type,
        status=campaign.status,
        cohort_definition=campaign.cohort_definition,
        start_date=campaign.start_date,
        end_date=campaign.end_date,
        created_at=campaign.created_at,
        enrolled_count=enrolled_count,
    )


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
