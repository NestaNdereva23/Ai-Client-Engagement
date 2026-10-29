from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.db.models.action_performance import UNKNOWN_LABEL
from app.db.models.action_result import ActionResult
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog, TouchPlan
from app.db.models.llmops import GenerationRun
from app.db.models.models import ClientFeatures
from app.db.models.outreach import Campaign, OutreachMessage

MIN_GROUP_SIZE = 20
TOO_FEW = "too few to tell"
NO_DATA = {
    "status": "not_enough_data",
    "message": "No finished campaign results are available yet.",
}
WORDING = (
    "Each count is what was seen after the email. A deposit seen within the window of days "
    "after an email is not proof the email caused it, and clients who were sent nothing are "
    "not compared yet. Each count is out of the clients we could check."
)
CUTS = ("campaign", "angle", "tier", "value_band")
FLAGS = {
    "opened": ActionResult.opened,
    "replied": ActionResult.replied,
    "bounced": ActionResult.bounced,
    "deposit_seen": ActionResult.deposited,
    "moved_to_active": ActionResult.moved_to_active,
    "balance_up": ActionResult.balance_up,
}


def campaign_results(session: Session) -> dict[str, Any]:
    rows = session.execute(_finished_touches()).all()
    if not rows:
        return dict(NO_DATA)

    groups: dict[str, dict[tuple[str, int], list[Any]]] = {cut: defaultdict(list) for cut in CUTS}
    for row in rows:
        for cut in CUTS:
            groups[cut][(getattr(row, cut), row.window_days)].append(row)
    return {
        "status": "ok",
        "note": WORDING,
        **{f"by_{cut}": _summaries(groups[cut]) for cut in CUTS},
    }


def _finished_touches() -> Select[Any]:
    return (
        select(
            Campaign.name.label("campaign"),
            func.coalesce(TouchPlan.angle, CampaignStep.message_angle, UNKNOWN_LABEL).label(
                "angle"
            ),
            func.coalesce(GenerationRun.priority_tier, UNKNOWN_LABEL).label("tier"),
            func.coalesce(ClientFeatures.value_band, UNKNOWN_LABEL).label("value_band"),
            ActionResult.window_days,
            *(column.label(name) for name, column in FLAGS.items()),
        )
        .select_from(ActionResult)
        .join(TouchLog, TouchLog.touch_id == ActionResult.touch_id)
        .join(Enrollment, Enrollment.enrollment_id == TouchLog.enrollment_id)
        .join(Campaign, Campaign.campaign_id == Enrollment.campaign_id)
        .outerjoin(
            TouchPlan,
            and_(
                TouchPlan.enrollment_id == TouchLog.enrollment_id,
                TouchPlan.step_no == TouchLog.step_no,
            ),
        )
        .outerjoin(
            CampaignStep,
            and_(
                CampaignStep.campaign_id == Enrollment.campaign_id,
                CampaignStep.step_no == TouchLog.step_no,
            ),
        )
        .outerjoin(OutreachMessage, OutreachMessage.message_id == ActionResult.message_id)
        .outerjoin(GenerationRun, GenerationRun.run_id == OutreachMessage.generation_run_id)
        .outerjoin(ClientFeatures, ClientFeatures.client_id == ActionResult.client_id)
    )


def _summaries(groups: dict[tuple[str, int], list[Any]]) -> list[dict[str, Any]]:
    ranked = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    return [_summary(label, window, rows) for (label, window), rows in ranked]


def _summary(label: str, window_days: int, rows: list[Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"label": label, "window_days": window_days, "sent": len(rows)}
    if len(rows) < MIN_GROUP_SIZE:
        return {**summary, "result": TOO_FEW}
    for name in FLAGS:
        known = [getattr(row, name) for row in rows if getattr(row, name) is not None]
        summary[name] = {"count": sum(known), "of": len(known)}
    return summary
