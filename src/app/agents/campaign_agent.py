from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.agent_loop import start_agent_run
from app.agents.campaign_plan_checks import (
    MAX_RESEARCH_NOTES,
    MAX_RETRIES,
    FactSource,
    PlanError,
    PlanRules,
    check_decisions,
    clean_text,
)
from app.agents.campaign_prompt import build_plan_prompt
from app.agents.prompt_versioning import CAMPAIGN_PLAN, prompt_text
from app.agents.run_cost import run_cost_kes
from app.agents.tool_runtime import make_tool_executor, wrapped_as_data
from app.agents.tools import PLANNING_TOOLS, make_campaign_read_tools, specs_for
from app.audit.log import record_audit
from app.campaigns.slot_context import (
    FIRST_TOUCH,
    NOTE_LIMIT,
    CampaignSlots,
    SlotContext,
    build_campaign_slots,
)
from app.config import get_settings
from app.db.models.agent_run import CAMPAIGN_AGENT, AgentRun, AgentToolCall
from app.db.models.campaigns import CampaignStep, TouchPlan
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
from app.rules.catalog import load_active_angles
from app.schemas.campaigns import CampaignPlanConfig

logger = structlog.get_logger(__name__)

CHOOSE_TOUCH_PLAN_TOOL_NAME = "choose_touch_plan"
DEFAULT_MAX_TURNS = 14
_FACT_TOOLS = ("search_web", "get_verified_rate")


class CampaignNotFound(Exception):
    pass


class CampaignPlanFailed(Exception):
    pass


def build_choose_touch_plan_tool_spec(
    *, slot_numbers: list[int], allowed_angles: list[str]
) -> ToolSpec:
    return ToolSpec(
        name=CHOOSE_TOUCH_PLAN_TOOL_NAME,
        description=(
            "Record this run's decision for every slot you were given: an angle "
            "from the menu that this client was not already sent, how many days "
            "until the next touch, a reason and a next_time note. Cover every slot "
            "in one call. If it is refused it lists every problem; fix them all "
            "and call again, at most two more times. Once it saves, reply with "
            "plain text and no further tool call."
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
                            "suggested_subject": {
                                "type": "string",
                                "description": "Optional. Must not be close to an earlier subject.",
                            },
                            "next_time": {
                                "type": "string",
                                "description": (
                                    "One short line for the next planning run: what to follow "
                                    "up on, or avoid, with this client."
                                ),
                            },
                            "research_notes": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    f"Up to {MAX_RESEARCH_NOTES} facts copied verbatim from "
                                    "search_web or get_verified_rate for this slot's drafting "
                                    "step to cite. Leave empty if you found nothing to cite."
                                ),
                            },
                        },
                        "required": ["slot", "angle", "gap_days", "reason", "next_time"],
                    },
                }
            },
            "required": ["decisions"],
        },
    )


def _clean_note(raw: Any) -> str | None:
    note = clean_text(raw)[:NOTE_LIMIT]
    return note or None


def _web_link(value: Any) -> str | None:
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else None


def _fact_sources(session: Session, run_id: int) -> list[FactSource]:
    calls = session.scalars(
        select(AgentToolCall)
        .where(AgentToolCall.run_id == run_id, AgentToolCall.tool_name.in_(_FACT_TOOLS))
        .order_by(AgentToolCall.ordinal)
    )
    sources: list[FactSource] = []
    for call in calls:
        output = call.tool_output or {}
        stamp = output.get("retrieved_at") or call.created_at.isoformat()
        if call.tool_name == "search_web":
            sources.extend(
                FactSource(
                    str(hit.get("snippet") or ""), _web_link(hit.get("url")), stamp, call.tool_name
                )
                for hit in output.get("results") or []
            )
        elif output.get("text"):
            sources.append(
                FactSource(output["text"], _web_link(output.get("source")), stamp, call.tool_name)
            )
    return sources


def _write_plans(
    session: Session,
    run_id: int,
    decisions: list[dict[str, Any]],
    slots_by_number: dict[int, dict[str, Any]],
    facts: dict[int, list[dict[str, Any]]],
) -> int:
    for decision in decisions:
        number = decision["slot"]
        slot = slots_by_number[number]
        row = TouchPlan(
            enrollment_id=slot["enrollment_id"],
            step_no=slot["step_no"],
            angle=decision["angle"],
            gap_days=decision["gap_days"],
            reason=clean_text(decision["reason"]),
            run_id=run_id,
            cited_facts=facts[number] or None,
            next_time_note=_clean_note(decision.get("next_time")),
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
    return len(decisions)


def make_choose_touch_plan_tool(
    *,
    slots: list[dict[str, Any]],
    allowed_angles: set[str],
    min_gap_days: int,
    max_gap_days: int,
    run_id: int,
    outcome: dict[str, Any],
    angle_families: dict[str, str | None] | None = None,
):
    slots_by_number = {slot["slot"]: slot for slot in slots}
    rules = PlanRules(allowed_angles, angle_families or {}, min_gap_days, max_gap_days)

    def reject(errors: list[PlanError]) -> dict[str, Any]:
        outcome["failed_tries"] = outcome.get("failed_tries", 0) + 1
        outcome["errors"] = [error.as_dict() for error in errors]
        return {
            "error": errors[0].code,
            "message": "; ".join(error.message for error in errors),
            "errors": outcome["errors"],
            "tries_left": max(MAX_RETRIES + 1 - outcome["failed_tries"], 0),
        }

    def choose_touch_plan(session: Session, *, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        if outcome.get("written"):
            return {"error": "already_saved", "message": "this run's plan is already saved"}
        if outcome.get("failed_tries", 0) > MAX_RETRIES:
            return {"error": "no_tries_left", "message": "no tries are left; reply in plain text"}
        if not isinstance(decisions, list) or not all(isinstance(d, dict) for d in decisions):
            message = "decisions must be a list of decision objects"
            return reject([PlanError("invalid_decisions", message)])

        sources = _fact_sources(session, run_id)
        errors, facts = check_decisions(decisions, slots_by_number, rules, sources)
        if errors:
            return reject(errors)

        try:
            with session.begin_nested():
                count = _write_plans(session, run_id, decisions, slots_by_number, facts)
        except IntegrityError:
            message = "a plan is already saved for one of these touches"
            return reject([PlanError("duplicate_plan", message)])

        outcome["written"] = count
        return {"status": "written", "count": count}

    return choose_touch_plan


def _slot_line(number: int, slot: SlotContext) -> str:
    parts = [f"slot {number}:"]
    parts.append("first touch" if slot.kind == FIRST_TOUCH else f"follow up, touch {slot.step_no}")
    if slot.kind != FIRST_TOUCH:
        parts.append(f"last angle {slot.prior_angle or 'not on record'}")
    if slot.earlier_touches and slot.earlier_touches[-1].days_since_sent is not None:
        parts.append(f"{slot.earlier_touches[-1].days_since_sent} days since the last touch")
    events = Counter(event.type for event in slot.events_since_last_touch)
    if events:
        parts.append("since then " + ", ".join(f"{n} {kind}" for kind, n in sorted(events.items())))
    if slot.next_time_note:
        parts.append(f"note from the last plan: {' '.join(slot.next_time_note.split())}")
    return " ".join(parts)


def _slots_prompt(slots: tuple[SlotContext, ...]) -> str:
    lines = [_slot_line(number, slot) for number, slot in enumerate(slots)]
    return "<slot_data>\n" + "\n".join(lines) + "\n</slot_data>"


def _left_out(contexts: CampaignSlots) -> str:
    reasons = contexts.dropped_reasons()
    if not reasons:
        return ""
    detail = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
    return f"left out {sum(reasons.values())} ({detail})"


def _finish_summary(headline: str, contexts: CampaignSlots) -> str:
    left_out = _left_out(contexts)
    return f"{headline}; {left_out}" if left_out else headline


def _planning_slots(ready: tuple[SlotContext, ...]) -> list[dict[str, Any]]:
    return [
        {
            "slot": number,
            "enrollment_id": slot.enrollment_id,
            "step_no": slot.step_no,
            "prior_angle": slot.prior_angle,
            "earlier_angles": [touch.angle for touch in slot.earlier_touches if touch.angle],
            "earlier_subjects": [touch.subject for touch in slot.earlier_touches if touch.subject],
        }
        for number, slot in enumerate(ready)
    ]


def _why_incomplete(outcome: dict[str, Any], saved: int, wanted: int, stopped: str) -> str:
    errors = outcome.get("errors") or []
    if errors:
        detail = "; ".join(error["message"] for error in errors)
        return f"plans refused after {outcome['failed_tries']} tries: {detail}"
    if stopped == "turn_limit":
        return "the agent ran out of turns before saving a plan"
    if not outcome.get("written"):
        return "the agent never saved a plan"
    return f"saved {saved} plans for {wanted} due touches"


def _require_saved_plans(
    session: Session, run_id: int, wanted: int, outcome: dict[str, Any], stopped: str
) -> None:
    saved = session.scalar(
        select(func.count()).select_from(TouchPlan).where(TouchPlan.run_id == run_id)
    )
    if saved != wanted or outcome.get("written") != wanted:
        raise CampaignPlanFailed(_why_incomplete(outcome, saved, wanted, stopped))


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
        contexts = build_campaign_slots(session, campaign_id)
        session.commit()
        if not contexts.ready:
            run.state = "completed"
            run.summary = (
                _finish_summary("nothing to plan", contexts) if contexts.dropped else "nothing due"
            )
            run.finished_at = datetime.now(UTC)
            session.commit()
            return run

        slots = _planning_slots(contexts.ready)
        plan_config = CampaignPlanConfig.model_validate(campaign.agent_touch_policy or {})
        min_gap_days, max_gap_days = plan_config.min_gap_days, plan_config.max_gap_days
        catalogue = load_active_angles(session, date.today())
        allowed_angles = {code for code, row in catalogue.items() if not row.held}
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
            angle_families={code: row.family for code, row in catalogue.items()},
        )
        tool_spec = build_choose_touch_plan_tool_spec(
            slot_numbers=[slot["slot"] for slot in slots], allowed_angles=sorted(allowed_angles)
        )
        base_prompt = prompt_text(session, CAMPAIGN_PLAN, date.today())
        system_prompt = build_plan_prompt(base_prompt, plan_config)
        system_prompt = f"{system_prompt}\n\nThis run's slots:\n{_slots_prompt(contexts.ready)}"
        touch_points = session.scalar(
            select(func.count())
            .select_from(CampaignStep)
            .where(CampaignStep.campaign_id == campaign_id)
        )
        read_tools = make_campaign_read_tools(
            plan_config=plan_config,
            touch_points=touch_points,
            allowed_angles=allowed_angles,
            slots=contexts.ready,
        )

        converse = as_converse_call(
            llm_client,
            system=system_prompt,
            tools=(*specs_for(PLANNING_TOOLS), tool_spec),
        )
        call_tool = wrapped_as_data(
            make_tool_executor(
                session,
                run_id,
                extra_tools={CHOOSE_TOUCH_PLAN_TOOL_NAME: choose_tool, **read_tools},
            )
        )

        tracer = tracer or active_tracer()
        with trace_scope(tracer, name=CAMPAIGN_AGENT_PLAN, tags=[TAG_AGENT, TAG_CAMPAIGN]):
            conversation = run_conversation_boundary(
                {},
                converse,
                call_tool,
                max_turns=DEFAULT_MAX_TURNS,
                run_id=str(run_id),
                trace_id=str(run_id),
            )

        _require_saved_plans(session, run_id, len(slots), outcome, conversation.stopped_reason)

        run.cost_kes = run_cost_kes(session, llm_client.model, date.today(), 1)
        run.summary = _finish_summary(
            f"planned {outcome.get('written', 0)} of {len(slots)} due touches", contexts
        )
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
