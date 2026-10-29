from __future__ import annotations

from typing import Any

from sqlalchemy import Select, and_, func, select, union
from sqlalchemy.orm import Session

from app.db.models.action_result import ActionResult
from app.db.models.agent_run import AgentRun, AgentToolCall
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog, TouchPlan
from app.db.models.outreach import OutreachMessage, ReviewAction

RESULTS_TOOL = "get_campaign_results"
DEFAULT_LIMIT = 200


def explain_campaign(
    session: Session, campaign_id: int, *, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    rows = session.execute(_touches(campaign_id, limit)).all()
    reviews = _latest_reviews(session, [row.message_id for row in rows if row.message_id])
    run_ids = sorted({row.run_id for row in rows if row.run_id is not None})
    return {
        "touches": [_touch(row, reviews.get(row.message_id)) for row in rows],
        "runs": _runs(session, run_ids),
    }


def _touches(campaign_id: int, limit: int) -> Select[Any]:
    slots = union(
        select(TouchPlan.enrollment_id, TouchPlan.step_no),
        select(TouchLog.enrollment_id, TouchLog.step_no),
    ).subquery("slot")
    return (
        select(
            slots.c.enrollment_id,
            slots.c.step_no,
            Enrollment.client_id,
            func.coalesce(TouchPlan.angle, CampaignStep.message_angle).label("angle"),
            TouchPlan.run_id,
            TouchPlan.gap_days,
            TouchPlan.reason,
            TouchPlan.next_time_note,
            TouchPlan.cited_facts,
            TouchLog.touch_id,
            TouchLog.message_id,
            TouchLog.sent_at,
            TouchLog.delivery_status,
            OutreachMessage.status.label("message_status"),
            OutreachMessage.ai_draft_content.label("draft"),
            ActionResult.window_days,
            ActionResult.opened,
            ActionResult.replied,
            ActionResult.bounced,
            ActionResult.deposited,
            ActionResult.deposit_amount_kes,
            ActionResult.moved_to_active,
            ActionResult.balance_up,
            ActionResult.measured_at,
        )
        .select_from(slots)
        .join(Enrollment, Enrollment.enrollment_id == slots.c.enrollment_id)
        .outerjoin(
            TouchPlan,
            and_(
                TouchPlan.enrollment_id == slots.c.enrollment_id,
                TouchPlan.step_no == slots.c.step_no,
            ),
        )
        .outerjoin(
            TouchLog,
            and_(
                TouchLog.enrollment_id == slots.c.enrollment_id,
                TouchLog.step_no == slots.c.step_no,
            ),
        )
        .outerjoin(
            CampaignStep,
            and_(
                CampaignStep.campaign_id == Enrollment.campaign_id,
                CampaignStep.step_no == slots.c.step_no,
            ),
        )
        .outerjoin(OutreachMessage, OutreachMessage.message_id == TouchLog.message_id)
        .outerjoin(ActionResult, ActionResult.touch_id == TouchLog.touch_id)
        .where(Enrollment.campaign_id == campaign_id)
        .order_by(slots.c.enrollment_id, slots.c.step_no)
        .limit(limit)
    )


def _latest_reviews(session: Session, message_ids: list[str]) -> dict[str, ReviewAction]:
    reviews = session.scalars(
        select(ReviewAction)
        .where(ReviewAction.message_id.in_(message_ids))
        .order_by(ReviewAction.review_action_id)
    )
    return {review.message_id: review for review in reviews}


def _touch(row: Any, review: ReviewAction | None) -> dict[str, Any]:
    draft = row.draft or {}
    return {
        "enrollment_id": row.enrollment_id,
        "client_id": row.client_id,
        "step_no": row.step_no,
        "angle": row.angle,
        "stage": _stage(row),
        "plan": _plan(row),
        "draft": None
        if row.message_id is None
        else {
            "message_id": row.message_id,
            "subject": draft.get("subject"),
            "body": draft.get("body"),
            "status": row.message_status,
        },
        "approval": None
        if review is None
        else {
            "outcome": review.outcome,
            "reviewer_id": review.reviewer_id,
            "decided_at": review.created_at,
        },
        "send": None
        if row.touch_id is None
        else {
            "touch_id": row.touch_id,
            "sent_at": row.sent_at,
            "delivery_status": row.delivery_status,
        },
        "outcome": _outcome(row),
    }


def _stage(row: Any) -> str:
    if row.window_days is not None:
        return "measured"
    if row.sent_at is not None:
        return "sent"
    return "drafted" if row.message_id is not None else "planned"


def _plan(row: Any) -> dict[str, Any] | None:
    if row.run_id is None:
        return None
    return {
        "run_id": row.run_id,
        "gap_days": row.gap_days,
        "reason": row.reason,
        "next_time_note": row.next_time_note,
        "cited_facts": row.cited_facts,
    }


def _outcome(row: Any) -> dict[str, Any] | None:
    if row.window_days is None:
        return None
    return {
        "window_days": row.window_days,
        "opened": row.opened,
        "replied": row.replied,
        "bounced": row.bounced,
        "deposit_seen": row.deposited,
        "deposit_amount_kes": row.deposit_amount_kes,
        "moved_to_active": row.moved_to_active,
        "balance_up": row.balance_up,
        "measured_at": row.measured_at,
    }


def _runs(session: Session, run_ids: list[int]) -> list[dict[str, Any]]:
    seen: dict[int, list[dict[str, Any]]] = {run_id: [] for run_id in run_ids}
    calls = session.scalars(
        select(AgentToolCall)
        .where(AgentToolCall.run_id.in_(run_ids), AgentToolCall.tool_name == RESULTS_TOOL)
        .order_by(AgentToolCall.run_id, AgentToolCall.ordinal)
    )
    for call in calls:
        seen[call.run_id].append({"called_at": call.created_at, "results": call.tool_output})
    runs = session.scalars(select(AgentRun).where(AgentRun.run_id.in_(run_ids)))
    return [
        {
            "run_id": run.run_id,
            "started_at": run.started_at,
            "state": run.state,
            "results_seen": seen[run.run_id],
        }
        for run in sorted(runs, key=lambda run: run.run_id)
    ]
