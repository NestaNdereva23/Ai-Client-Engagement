from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.agent_loop import start_agent_run
from app.agents.run_cost import run_cost_kes
from app.agents.tool_runtime import make_tool_executor
from app.agents.tools import TOOL_SPECS
from app.audit.log import record_audit
from app.campaigns.scheduler import select_due_enrollments
from app.config import get_settings
from app.db.models.agent_run import CAMPAIGN_AGENT, AgentRun
from app.db.models.campaigns import TouchPlan
from app.db.models.outreach import Campaign
from app.llmops.operations import active_tracer
from app.llmops.trace_names import CAMPAIGN_AGENT_PLAN, TAG_AGENT, TAG_CAMPAIGN
from app.llmops.tracing import Tracer, trace_scope
from app.privacy.boundary import run_conversation_boundary
from app.privacy.llm_client import (
    ConversingLLMClient,
    ToolSpec,
    as_converse_call,
    get_agent_llm_client,
)
from app.rules.catalog import live_angle_codes
from app.schemas.campaigns import CampaignPlanConfig

logger = structlog.get_logger(__name__)

CHOOSE_TOUCH_PLAN_TOOL_NAME = "choose_touch_plan"
DEFAULT_MAX_TURNS = 10
_MAX_RESEARCH_NOTES = 3
_RESEARCH_NOTE_LIMIT = 500

_LIST_ANGLES_SPEC = next(spec for spec in TOOL_SPECS if spec.name == "list_angles")
_SEARCH_WEB_SPEC = next(spec for spec in TOOL_SPECS if spec.name == "search_web")
_GET_VERIFIED_RATE_SPEC = next(spec for spec in TOOL_SPECS if spec.name == "get_verified_rate")


class CampaignNotFound(Exception):
    pass


def _gather_slots(session: Session, campaign_id: int) -> list[dict[str, Any]]:
    due = select_due_enrollments(session, campaign_id=campaign_id)
    slots = []
    for i, enrollment in enumerate(due):
        step_no = enrollment.current_step + 1
        prior = session.execute(
            select(TouchPlan.angle).where(
                TouchPlan.enrollment_id == enrollment.enrollment_id,
                TouchPlan.step_no == step_no - 1,
            )
        ).scalar_one_or_none()
        slots.append(
            {
                "slot": i,
                "enrollment_id": enrollment.enrollment_id,
                "step_no": step_no,
                "prior_angle": prior,
            }
        )
    return slots


def build_choose_touch_plan_tool_spec(
    *, slot_numbers: list[int], allowed_angles: list[str]
) -> ToolSpec:
    return ToolSpec(
        name=CHOOSE_TOUCH_PLAN_TOOL_NAME,
        description=(
            "Record this run's decision for every slot you were given: an angle "
            "from the menu that is not that slot's prior angle, and how many "
            "days until the next touch. Call this once, covering every slot, "
            "then reply with plain text and no further tool call."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "slot": {"type": "integer", "enum": sorted(slot_numbers)},
                            "angle": {"type": "string", "enum": sorted(allowed_angles)},
                            "gap_days": {"type": "integer"},
                            "reason": {"type": "string"},
                            "research_notes": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    f"Up to {_MAX_RESEARCH_NOTES} facts copied verbatim from "
                                    "search_web or get_verified_rate for this slot's drafting "
                                    "step to cite. Leave empty if you found nothing to cite."
                                ),
                            },
                        },
                        "required": ["slot", "angle", "gap_days", "reason"],
                    },
                }
            },
            "required": ["decisions"],
        },
    )


def _clean_research_notes(raw: Any) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    notes = [str(note).strip()[:_RESEARCH_NOTE_LIMIT] for note in raw[:_MAX_RESEARCH_NOTES]]
    cleaned = [note for note in notes if note]
    return cleaned or None


def make_choose_touch_plan_tool(
    *,
    slots: list[dict[str, Any]],
    allowed_angles: set[str],
    min_gap_days: int,
    max_gap_days: int,
    run_id: int,
    outcome: dict[str, Any],
):
    slots_by_number = {slot["slot"]: slot for slot in slots}

    def choose_touch_plan(session: Session, *, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        seen_slots: set[int] = set()
        for decision in decisions:
            slot_number = decision.get("slot")
            slot = slots_by_number.get(slot_number)
            if slot is None:
                message = f"slot {slot_number!r} was not one of this run's due touches"
                return {"error": "unknown_slot", "message": message}
            if slot_number in seen_slots:
                message = f"slot {slot_number} was decided more than once in this call"
                return {"error": "duplicate_slot", "message": message}
            seen_slots.add(slot_number)

            angle = decision.get("angle")
            if angle not in allowed_angles:
                message = f"{angle!r} is not a live, open angle"
                return {"error": "unknown_angle", "message": message}
            if angle == slot.get("prior_angle"):
                message = f"slot {slot_number} was already sent {angle!r} last time"
                return {"error": "repeats_last_angle", "message": message}

            gap_days = decision.get("gap_days")
            if not isinstance(gap_days, int) or not (min_gap_days <= gap_days <= max_gap_days):
                message = f"gap_days must be between {min_gap_days} and {max_gap_days}"
                return {"error": "gap_out_of_range", "message": message}

            if not str(decision.get("reason") or "").strip():
                message = f"slot {slot_number} needs a short reason"
                return {"error": "missing_reason", "message": message}

        if seen_slots != set(slots_by_number):
            missing = sorted(set(slots_by_number) - seen_slots)
            message = f"slots {missing} still need a decision"
            return {"error": "incomplete", "message": message}

        written = []
        for decision in decisions:
            slot = slots_by_number[decision["slot"]]
            row = TouchPlan(
                enrollment_id=slot["enrollment_id"],
                step_no=slot["step_no"],
                angle=decision["angle"],
                gap_days=decision["gap_days"],
                reason=decision["reason"].strip(),
                run_id=run_id,
                cited_facts=_clean_research_notes(decision.get("research_notes")),
            )
            session.add(row)
            session.flush()
            record_audit(
                session,
                entity_type="touch_plan",
                action="write",
                entity_id=str(row.plan_id),
                run_id=str(run_id),
                detail={
                    "enrollment_id": row.enrollment_id,
                    "step_no": row.step_no,
                    "angle": row.angle,
                    "gap_days": row.gap_days,
                    "cited_facts": row.cited_facts,
                },
            )
            written.append(row.plan_id)

        outcome["written"] = len(written)
        return {"status": "written", "count": len(written)}

    return choose_touch_plan


_SYSTEM_PROMPT = (
    "You plan the next email touch for a dormant win-back campaign.\n"
    "You were given a list of slots through list_angles and the choose_touch_plan "
    "tool's own schema; each slot is one client's due touch, identified only by "
    "slot number, with the angle (if any) they were sent last time.\n"
    "Pick an angle from list_angles for every slot: never the slot's prior angle, "
    "and never a held angle. Do not default to one angle for every slot that has "
    "no prior angle: spread first-touch slots across different live angles, the "
    "same way you would if they all had history to avoid repeating. Only repeat "
    "an angle across slots when the catalogue genuinely leaves you no other fit. "
    "Pick how many days until that client's next touch, "
    "between {min_gap} and {max_gap} days; it does not have to be the same for "
    "every slot or every run.\n"
    "You may call search_web for a fact from an approved source, and "
    "get_verified_rate for the product's own current rate. Never invent a fact "
    "or a rate yourself. If you find something worth the drafting step citing "
    "for a slot, copy it verbatim into that slot's research_notes; leave it "
    "empty otherwise.\n"
    "Call choose_touch_plan exactly once, covering every slot you were given, "
    "then reply with a short plain summary and no further tool call."
)


def _slots_prompt(slots: list[dict[str, Any]]) -> str:
    lines = [
        f"slot {slot['slot']}: prior angle {slot['prior_angle'] or 'none, first touch'}"
        for slot in slots
    ]
    return "\n".join(lines)


def run_campaign_agent(
    session: Session,
    campaign_id: int,
    *,
    trigger: str = "manual",
    llm_client: ConversingLLMClient | None = None,
    tracer: Tracer | None = None,
) -> AgentRun:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFound(campaign_id)

    settings = get_settings()
    run = start_agent_run(session, trigger=trigger, kind=CAMPAIGN_AGENT)
    run_id = run.run_id

    try:
        slots = _gather_slots(session, campaign_id)
        if not slots:
            run.state = "completed"
            run.summary = "nothing due"
            run.finished_at = datetime.now(UTC)
            session.commit()
            return run

        plan_config = CampaignPlanConfig.model_validate(campaign.agent_touch_policy or {})
        min_gap_days, max_gap_days = plan_config.min_gap_days, plan_config.max_gap_days
        allowed_angles = live_angle_codes(session, date.today())
        if plan_config.allowed_angles:
            allowed_angles &= set(plan_config.allowed_angles)

        llm_client = llm_client or get_agent_llm_client(settings)
        outcome: dict[str, Any] = {}
        choose_tool = make_choose_touch_plan_tool(
            slots=slots,
            allowed_angles=allowed_angles,
            min_gap_days=min_gap_days,
            max_gap_days=max_gap_days,
            run_id=run_id,
            outcome=outcome,
        )
        tool_spec = build_choose_touch_plan_tool_spec(
            slot_numbers=[slot["slot"] for slot in slots], allowed_angles=sorted(allowed_angles)
        )
        system_prompt = _SYSTEM_PROMPT.format(min_gap=min_gap_days, max_gap=max_gap_days)
        system_prompt = f"{system_prompt}\n\nThis run's slots:\n{_slots_prompt(slots)}"

        converse = as_converse_call(
            llm_client,
            system=system_prompt,
            tools=(_LIST_ANGLES_SPEC, _SEARCH_WEB_SPEC, _GET_VERIFIED_RATE_SPEC, tool_spec),
        )
        call_tool = make_tool_executor(
            session, run_id, extra_tools={CHOOSE_TOUCH_PLAN_TOOL_NAME: choose_tool}
        )

        tracer = tracer or active_tracer()
        with trace_scope(tracer, name=CAMPAIGN_AGENT_PLAN, tags=[TAG_AGENT, TAG_CAMPAIGN]):
            run_conversation_boundary(
                {},
                converse,
                call_tool,
                max_turns=DEFAULT_MAX_TURNS,
                run_id=str(run_id),
                trace_id=str(run_id),
            )

        run.cost_kes = run_cost_kes(session, llm_client.model, date.today(), 1)
        run.summary = f"planned {outcome.get('written', 0)} of {len(slots)} due touches"
        run.state = "completed"
        run.finished_at = datetime.now(UTC)
        session.commit()
        return run
    except Exception as exc:
        session.rollback()
        run = session.get(AgentRun, run_id)
        run.state = "failed"
        run.failure_reason = str(exc)
        run.finished_at = datetime.now(UTC)
        session.commit()
        raise
