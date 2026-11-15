from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.campaigns import CampaignStep
from app.db.models.outreach import AGENT_PLANNING_MODE, AGENT_PROPOSAL_CAMPAIGN_TYPE, Campaign
from app.rules.catalog import live_angle_codes
from app.schemas.campaigns import DEFAULT_MAX_GAP_DAYS, DEFAULT_MIN_GAP_DAYS


class CampaignNotReady(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("campaign is not ready: " + "; ".join(problems))


@dataclass(frozen=True)
class ReadinessCheck:
    label: str
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


def campaign_readiness_checks(session: Session, campaign: Campaign) -> list[ReadinessCheck]:
    if campaign.planning_mode != AGENT_PLANNING_MODE:
        return []

    config = campaign.agent_touch_policy or {}
    steps = session.scalar(
        select(func.count())
        .select_from(CampaignStep)
        .where(CampaignStep.campaign_id == campaign.campaign_id)
    )
    not_live = set(config.get("allowed_angles") or []) - live_angle_codes(session, date.today())
    min_gap = config.get("min_gap_days", DEFAULT_MIN_GAP_DAYS)
    max_gap = config.get("max_gap_days", DEFAULT_MAX_GAP_DAYS)
    gap_problems = (
        (f"the shortest gap ({min_gap} days) is above the longest ({max_gap} days)",)
        if min_gap > max_gap
        else ()
    )
    return [
        ReadinessCheck(
            "Objective set", () if config.get("objective") else ("no objective is set",)
        ),
        ReadinessCheck(
            f"{steps} touch point{'' if steps == 1 else 's'}",
            () if steps else ("no touch points are set",),
        ),
        ReadinessCheck(
            "All angles live",
            tuple(f"angle {angle} is not in the live catalogue" for angle in sorted(not_live)),
        ),
        ReadinessCheck(f"Gap {min_gap} to {max_gap} days", gap_problems),
        ReadinessCheck(
            "Audience is an inactive group",
            ("the audience is not an inactive group",)
            if campaign.campaign_type == AGENT_PROPOSAL_CAMPAIGN_TYPE
            else (),
        ),
    ]


def campaign_readiness_problems(session: Session, campaign: Campaign) -> list[str]:
    return [
        problem
        for check in campaign_readiness_checks(session, campaign)
        for problem in check.problems
    ]


def require_campaign_ready(session: Session, campaign: Campaign) -> None:
    problems = campaign_readiness_problems(session, campaign)
    if problems:
        raise CampaignNotReady(problems)
