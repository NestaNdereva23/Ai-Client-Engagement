from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.campaigns import CampaignStep
from app.db.models.outreach import AGENT_PLANNING_MODE, AGENT_PROPOSAL_CAMPAIGN_TYPE, Campaign
from app.rules.catalog import live_angle_codes
from app.schemas.campaigns import DEFAULT_MAX_GAP_DAYS, DEFAULT_MIN_GAP_DAYS


class CampaignNotReady(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("campaign is not ready: " + "; ".join(problems))


def campaign_readiness_problems(session: Session, campaign: Campaign) -> list[str]:
    if campaign.planning_mode != AGENT_PLANNING_MODE:
        return []

    config = campaign.agent_touch_policy or {}
    problems = []
    if not config.get("objective"):
        problems.append("no objective is set")

    has_steps = session.scalar(
        select(CampaignStep.step_id)
        .where(CampaignStep.campaign_id == campaign.campaign_id)
        .limit(1)
    )
    if has_steps is None:
        problems.append("no touch points are set")

    not_live = set(config.get("allowed_angles") or []) - live_angle_codes(session, date.today())
    problems.extend(f"angle {angle} is not in the live catalogue" for angle in sorted(not_live))

    min_gap = config.get("min_gap_days", DEFAULT_MIN_GAP_DAYS)
    max_gap = config.get("max_gap_days", DEFAULT_MAX_GAP_DAYS)
    if min_gap > max_gap:
        problems.append(f"the shortest gap ({min_gap} days) is above the longest ({max_gap} days)")

    if campaign.campaign_type == AGENT_PROPOSAL_CAMPAIGN_TYPE:
        problems.append("the audience is not an inactive group")
    return problems


def require_campaign_ready(session: Session, campaign: Campaign) -> None:
    problems = campaign_readiness_problems(session, campaign)
    if problems:
        raise CampaignNotReady(problems)
