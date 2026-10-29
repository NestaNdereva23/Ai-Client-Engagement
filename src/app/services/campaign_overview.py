from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, aliased

from app.campaigns.readiness import campaign_readiness_checks
from app.campaigns.results import campaign_results
from app.config import get_settings
from app.db.models.campaign_agent_run import PLAN_RUN, CampaignAgentRun, CampaignAgentToolCall
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog, TouchPlan
from app.db.models.models import ClientFeatures
from app.db.models.outreach import Campaign, OutreachMessage, ReviewCohort
from app.db.models.routing import ClientSide
from app.routing.sides import SIDE_INACTIVE
from app.rules.catalog import live_angle_codes, load_active_angles
from app.schemas.campaigns import CampaignPlanConfig
from app.services.campaign_suggestions import live_agent_campaign_count
from app.services.campaigns import CampaignNotFound

RECENT_RUNS = 10
PLAN_LIMIT = 50
FACT_LIMIT = 8
BAND_ORDER = ("Low", "Medium", "High", "Top")


def campaign_agent_overview(session: Session, campaign_id: int) -> dict[str, Any]:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFound(campaign_id)

    runs = _recent_runs(session, campaign_id)
    plan_run_id = session.scalar(
        select(func.max(TouchPlan.run_id))
        .join(Enrollment, Enrollment.enrollment_id == TouchPlan.enrollment_id)
        .where(Enrollment.campaign_id == campaign_id)
    )
    return {
        "campaign": _facts(session, campaign),
        "checks": [
            {"label": check.label, "ok": check.ok, "problems": list(check.problems)}
            for check in campaign_readiness_checks(session, campaign)
        ],
        "latest_run": runs[0] if runs else None,
        "recent_runs": runs,
        "plan_run_id": plan_run_id,
        "planned": _planned_touches(session, campaign_id, plan_run_id),
        "review": _review(session, campaign_id),
        "tools_used": _tools_used(session, plan_run_id),
        "cited_facts": _cited_facts(session, plan_run_id),
        "results": campaign_results(session, campaign_id),
        "group": _group(session, campaign),
        "live_agent_campaigns": {
            "live": live_agent_campaign_count(session),
            "allowed": get_settings().agent_campaign_max_live,
        },
    }


def _count(session: Session, column: Any, value: int) -> int:
    return session.scalar(select(func.count()).where(column == value)) or 0


def _facts(session: Session, campaign: Campaign) -> dict[str, Any]:
    config = CampaignPlanConfig.model_validate(campaign.agent_touch_policy or {})
    return {
        "name": campaign.name,
        "status": campaign.status,
        "planning_mode": campaign.planning_mode,
        "is_test": campaign.is_test,
        "objective": config.objective,
        "success_measure": config.success_measure,
        "observation_days": config.observation_days,
        "min_gap_days": config.min_gap_days,
        "max_gap_days": config.max_gap_days,
        "cohort": campaign.cohort_definition,
        "enrolled": _count(session, Enrollment.campaign_id, campaign.campaign_id),
        "touch_points": _count(session, CampaignStep.campaign_id, campaign.campaign_id),
        "allowed_angle_count": len(config.allowed_angles) or None,
        "live_angle_count": len(live_angle_codes(session, date.today())),
    }


def _recent_runs(session: Session, campaign_id: int) -> list[dict[str, Any]]:
    runs = session.scalars(
        select(CampaignAgentRun)
        .where(CampaignAgentRun.kind == PLAN_RUN, CampaignAgentRun.campaign_id == campaign_id)
        .order_by(CampaignAgentRun.run_id.desc())
        .limit(RECENT_RUNS)
    ).all()
    saved = dict(
        session.execute(
            select(TouchPlan.run_id, func.count())
            .where(TouchPlan.run_id.in_([run.run_id for run in runs]))
            .group_by(TouchPlan.run_id)
        ).all()
    )
    return [_run_row(run, saved.get(run.run_id, 0)) for run in runs]


def _run_row(run: CampaignAgentRun, saved: int) -> dict[str, Any]:
    report = run.plan_report or {}
    return {
        "run_id": run.run_id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "trigger": run.trigger,
        "state": run.state,
        "summary": run.summary,
        "failure_reason": run.failure_reason,
        "cost_kes": run.cost_kes,
        "due": report.get("due"),
        "planned": report.get("planned", saved),
        "left_out": report.get("left_out", {}),
        "rejections": report.get("rejections", []),
    }


def _planned_touches(session: Session, campaign_id: int, run_id: int | None) -> dict[str, Any]:
    if run_id is None:
        return {"total": 0, "touches": []}

    touch_points = _count(session, CampaignStep.campaign_id, campaign_id)
    families = {code: row.family for code, row in load_active_angles(session, date.today()).items()}
    before = aliased(TouchPlan)
    before_step = aliased(CampaignStep)
    before_touch = aliased(TouchLog)
    before_message = aliased(OutreachMessage)
    rows = session.execute(
        select(
            TouchPlan,
            Enrollment.client_id,
            ClientFeatures.value_band,
            func.coalesce(before.angle, before_step.message_angle).label("last_angle"),
            before_message.ai_draft_content["subject"].astext.label("last_subject"),
        )
        .join(Enrollment, Enrollment.enrollment_id == TouchPlan.enrollment_id)
        .outerjoin(ClientFeatures, ClientFeatures.client_id == Enrollment.client_id)
        .outerjoin(
            before,
            and_(
                before.enrollment_id == TouchPlan.enrollment_id,
                before.step_no == TouchPlan.step_no - 1,
            ),
        )
        .outerjoin(
            before_step,
            and_(
                before_step.campaign_id == Enrollment.campaign_id,
                before_step.step_no == TouchPlan.step_no - 1,
            ),
        )
        .outerjoin(
            before_touch,
            and_(
                before_touch.enrollment_id == TouchPlan.enrollment_id,
                before_touch.step_no == TouchPlan.step_no - 1,
            ),
        )
        .outerjoin(before_message, before_message.message_id == before_touch.message_id)
        .where(TouchPlan.run_id == run_id, Enrollment.campaign_id == campaign_id)
        .order_by(TouchPlan.plan_id)
        .limit(PLAN_LIMIT)
    ).all()
    total = session.scalar(
        select(func.count()).select_from(TouchPlan).where(TouchPlan.run_id == run_id)
    )
    return {
        "total": total,
        "touches": [
            {
                "client_id": client_id,
                "band": band,
                "step_no": plan.step_no,
                "touch_points": touch_points,
                "last_angle": last_angle,
                "last_subject": last_subject,
                "angle": plan.angle,
                "family": families.get(plan.angle),
                "suggested_subject": plan.suggested_subject,
                "gap_days": plan.gap_days,
                "reason": plan.reason,
                "next_time_note": plan.next_time_note,
            }
            for plan, client_id, band, last_angle, last_subject in rows
        ],
    }


def _review(session: Session, campaign_id: int) -> dict[str, Any]:
    rows = session.execute(
        select(OutreachMessage.is_sample, OutreachMessage.status, func.count())
        .where(OutreachMessage.campaign_id == campaign_id, OutreachMessage.cohort_id.is_not(None))
        .group_by(OutreachMessage.is_sample, OutreachMessage.status)
    ).all()
    counts = {(is_sample, status): number for is_sample, status, number in rows}
    ready = session.scalars(
        select(ReviewCohort.cohort_id).where(
            ReviewCohort.campaign_id == campaign_id,
            ReviewCohort.status == "ready_to_approve_rest",
        )
    ).all()
    return {
        "samples": sum(number for (is_sample, _), number in counts.items() if is_sample),
        "approved": counts.get((True, "approved"), 0),
        "waiting": counts.get((True, "pending_review"), 0),
        "others_waiting": counts.get((False, "pending_review"), 0),
        "ready_cohorts": list(ready),
    }


def _tools_used(session: Session, run_id: int | None) -> list[dict[str, Any]]:
    if run_id is None:
        return []
    rows = session.execute(
        select(CampaignAgentToolCall.tool_name, func.count())
        .where(CampaignAgentToolCall.run_id == run_id)
        .group_by(CampaignAgentToolCall.tool_name)
        .order_by(func.min(CampaignAgentToolCall.ordinal))
    ).all()
    return [{"name": name, "calls": calls} for name, calls in rows]


def _cited_facts(session: Session, run_id: int | None) -> list[dict[str, Any]]:
    if run_id is None:
        return []
    seen: dict[tuple[str, str | None], dict[str, Any]] = {}
    for facts in session.scalars(
        select(TouchPlan.cited_facts).where(
            TouchPlan.run_id == run_id, TouchPlan.cited_facts.is_not(None)
        )
    ):
        for fact in facts:
            seen.setdefault((fact["text"], fact.get("url")), fact)
    return list(seen.values())[:FACT_LIMIT]


def _group(session: Session, campaign: Campaign) -> dict[str, Any] | None:
    cohort = campaign.cohort_definition or {}
    tier = cohort.get("priority_tier")
    if not tier:
        return None
    rows = session.execute(
        select(ClientFeatures.value_band, func.count())
        .join(ClientSide, ClientSide.client_id == ClientFeatures.client_id)
        .where(ClientSide.side == SIDE_INACTIVE, ClientFeatures.priority_tier == tier)
        .group_by(ClientFeatures.value_band)
    ).all()
    counts = dict(rows)
    names = [
        *(name for name in BAND_ORDER if name in counts),
        *sorted(set(counts) - set(BAND_ORDER)),
    ]
    return {
        "tier": tier,
        "campaign_band": cohort.get("value_band"),
        "total": sum(counts.values()),
        "bands": [{"name": name, "count": counts[name]} for name in names],
    }
