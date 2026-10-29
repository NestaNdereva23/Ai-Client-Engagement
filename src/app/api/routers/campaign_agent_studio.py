from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.campaigns.agent_information import active_weekly_report
from app.db.session import get_session
from app.schemas.campaigns import ActiveWeeklyReportOut, CampaignToolOut
from app.services.campaign_agent_studio import campaign_tool_catalog

router = APIRouter(
    prefix="/campaign-agent-studio",
    tags=["campaign_agent_studio"],
    dependencies=[Depends(get_current_reviewer_id)],
)


@router.get("/weekly-report", response_model=ActiveWeeklyReportOut | None)
def get_active_weekly_report(
    session: Session = Depends(get_session),
) -> ActiveWeeklyReportOut | None:
    report = active_weekly_report(session)
    return ActiveWeeklyReportOut(**report) if report else None


@router.get("/tools", response_model=list[CampaignToolOut])
def get_campaign_tools() -> list[CampaignToolOut]:
    return [CampaignToolOut(**entry) for entry in campaign_tool_catalog()]
