from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.campaign_runs import start_campaign_run
from app.agents.campaign_tool_runner import make_campaign_tool_runner
from app.agents.run_cost import run_cost_kes
from app.agents.tools import SETUP_TOOLS, TOOL_FUNCTIONS, get_campaign_results, specs_for
from app.campaigns.waiting_pool import waiting_pool_summary
from app.config import get_settings
from app.db.models.campaign_agent_run import (
    SUGGEST_RUN,
    CampaignAgentRun,
    CampaignAgentToolCall,
)
from app.db.models.outreach import SUGGESTION_PENDING, Campaign
from app.db.session import SessionLocal
from app.llmops.operations import active_tracer
from app.llmops.trace_names import CAMPAIGN_AGENT_SUGGEST, TAG_AGENT, TAG_CAMPAIGN
from app.llmops.tracing import Tracer, get_shared_tracer, trace_scope
from app.privacy.boundary import run_conversation_boundary
from app.privacy.llm_client import (
    ConversingLLMClient,
    ToolSpec,
    as_converse_call,
    get_campaign_agent_llm_client,
)
from app.rules.catalog import live_angle_codes
from app.schemas.campaigns import CampaignDraftIn
from app.services.campaign_suggestions import (
    open_on_own,
    save_suggested_campaign,
    waiting_for_cohort,
)

logger = structlog.get_logger(__name__)

AUDIENCE_TOOL_NAME = "get_waiting_audience"
SAVE_DRAFT_TOOL_NAME = "save_campaign_draft"
DEFAULT_MAX_TURNS = 14
MAX_DRAFTS = 8

_OBJECTIVES = ["win_back_deposit", "top_up", "reactivate_never_funded"]


class NothingSaved(Exception):
    pass


def build_audience_tool_spec() -> ToolSpec:
    return ToolSpec(
        name=AUDIENCE_TOOL_NAME,
        description=(
            "Counts of the dormant clients who are not in any campaign yet, by priority "
            "tier, message angle and value band. Counts only; groups too small to show "
            "are left out."
        ),
        input_schema={"type": "object", "properties": {}},
    )


def build_save_draft_tool_spec() -> ToolSpec:
    return ToolSpec(
        name=SAVE_DRAFT_TOOL_NAME,
        description=(
            "Save one suggested campaign for a person to review. Nobody is contacted "
            "and nobody is enrolled until a person approves it. Give at least one "
            "group filter; use the names shown by get_waiting_audience."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "reason": {"type": "string", "description": "One plain line for the reviewer."},
                "objective": {"type": "string", "enum": _OBJECTIVES},
                "planning_mode": {"type": "string", "enum": ["agent", "fixed_steps"]},
                "touch_offsets": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Days after the start for each email, from 0, rising.",
                },
                "priority_tier": {"type": "string"},
                "value_band": {"type": "string"},
                "recency_band": {"type": "string"},
                "purchase_depth": {"type": "string"},
                "newly_dormant": {"type": "boolean"},
                "high_value": {"type": "boolean"},
                "min_gap_days": {"type": "integer"},
                "max_gap_days": {"type": "integer"},
                "allowed_angles": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name", "objective", "touch_offsets"],
        },
    )


def make_audience_tool(summary: dict[str, Any]):
    def get_waiting_audience(session: Session) -> dict[str, Any]:
        return summary

    return get_waiting_audience


def _plain_errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}".lstrip(": ")
        for error in exc.errors()
    )


def make_save_draft_tool(*, run_id: int, saved: list[int], covered: set[int]):
    def save_campaign_draft(
        session: Session,
        *,
        name: str,
        objective: str,
        touch_offsets: list[int],
        reason: str = "",
        planning_mode: str = "agent",
        priority_tier: str | None = None,
        value_band: str | None = None,
        recency_band: str | None = None,
        purchase_depth: str | None = None,
        newly_dormant: bool | None = None,
        high_value: bool | None = None,
        min_gap_days: int | None = None,
        max_gap_days: int | None = None,
        allowed_angles: list[str] | None = None,
    ) -> dict[str, Any]:
        if len(saved) >= MAX_DRAFTS:
            return {"error": "too_many_drafts", "message": f"at most {MAX_DRAFTS} drafts a run"}

        cohort = {
            "priority_tier": priority_tier,
            "value_band": value_band,
            "recency_band": recency_band,
            "purchase_depth": purchase_depth,
            "newly_dormant": newly_dormant,
            "high_value": high_value,
        }
        plan = {
            "objective": objective,
            "min_gap_days": min_gap_days,
            "max_gap_days": max_gap_days,
            "allowed_angles": allowed_angles,
        }
        try:
            draft = CampaignDraftIn(
                name=name,
                reason=reason,
                planning_mode=planning_mode,
                cohort={key: value for key, value in cohort.items() if value is not None},
                plan_config={key: value for key, value in plan.items() if value is not None},
                touch_offsets=touch_offsets,
            )
        except ValidationError as exc:
            return {"error": "invalid_draft", "message": _plain_errors(exc)}

        not_live = set(draft.plan_config.allowed_angles) - live_angle_codes(session, date.today())
        if not_live:
            return {
                "error": "unknown_angle",
                "message": f"not live angles: {sorted(not_live)}",
            }

        same_group = session.scalar(
            select(Campaign.campaign_id)
            .where(
                Campaign.suggestion_state == SUGGESTION_PENDING,
                Campaign.cohort_definition == draft.cohort.model_dump(),
            )
            .limit(1)
        )
        if same_group is not None:
            return {"error": "already_suggested", "message": "this group already has a draft"}

        waiting = waiting_for_cohort(session, draft.cohort.model_dump())
        if not waiting:
            return {"error": "nobody_waiting", "message": "no waiting client matches this group"}

        campaign = save_suggested_campaign(session, draft, run_id=run_id)
        saved.append(campaign.campaign_id)
        covered.update(waiting)
        return {"status": "saved", "draft_number": len(saved), "waiting_clients": len(waiting)}

    return save_campaign_draft


_SYSTEM_PROMPT = (
    "You suggest win-back campaigns for dormant clients who are not in any campaign "
    "yet. A person reviews every suggestion before anyone is contacted.\n"
    "Call get_waiting_audience first. It gives counts only, by priority tier, "
    "message angle and value band. Counts for groups that are too small are left "
    "out; do not guess them. Use list_angles to see what each angle may say.\n"
    "A campaign is one group of dormant clients, set by priority_tier and, when it "
    "helps, narrowed by value_band, recency_band, purchase_depth, newly_dormant or "
    "high_value. Never build a campaign around one message angle: the planning "
    "agent picks the angle for each client later.\n"
    "Try to cover the whole waiting pool: every priority tier that is shown gets a "
    "campaign. Split a very large tier by value_band only when that makes the "
    "groups easier to review.\n"
    "Save each campaign with save_campaign_draft, once. Choose an objective: "
    "win_back_deposit (bring back clients who used to deposit), top_up (clients "
    "who still hold money and could add to it) or reactivate_never_funded "
    "(clients who never funded). Give touch_offsets as the days after the start "
    "for each email, from 0 and rising, for example [0, 7, 14]. Write a short "
    "plain name and one plain line of reason.\n"
    "Save at most {max_drafts} drafts. If a save is refused, read the message and "
    "fix it. When you are done, reply with a short plain summary and no further "
    "tool call."
)


def start_suggestion_run(session: Session) -> CampaignAgentRun:
    get_campaign_agent_llm_client(get_settings())
    return start_campaign_run(session, kind=SUGGEST_RUN)


def _nothing_saved_reason(session: Session, run_id: int) -> str:
    calls = session.scalar(
        select(func.count())
        .select_from(CampaignAgentToolCall)
        .where(CampaignAgentToolCall.run_id == run_id)
    )
    return (
        f"the model saved no drafts and made {calls} tool calls; "
        "a model that cannot call tools does this, so pick another one in Settings"
    )


def execute_suggestion_run(
    session: Session,
    run_id: int,
    *,
    llm_client: ConversingLLMClient | None = None,
    tracer: Tracer | None = None,
) -> CampaignAgentRun:
    run = session.get(CampaignAgentRun, run_id)
    try:
        summary = waiting_pool_summary(session)
        if summary["total"] == 0:
            run.state = "completed"
            run.summary = "nobody is waiting"
            run.finished_at = datetime.now(UTC)
            session.commit()
            return run

        llm_client = llm_client or get_campaign_agent_llm_client(get_settings())
        saved: list[int] = []
        covered: set[int] = set()
        converse = as_converse_call(
            llm_client,
            system=_SYSTEM_PROMPT.format(max_drafts=MAX_DRAFTS),
            tools=(
                *specs_for(SETUP_TOOLS, extra=(build_audience_tool_spec(),)),
                build_save_draft_tool_spec(),
            ),
        )
        call_tool = make_campaign_tool_runner(
            session,
            run_id,
            {
                AUDIENCE_TOOL_NAME: make_audience_tool(summary),
                "get_campaign_results": get_campaign_results,
                "list_angles": TOOL_FUNCTIONS["list_angles"],
                SAVE_DRAFT_TOOL_NAME: make_save_draft_tool(
                    run_id=run_id, saved=saved, covered=covered
                ),
            },
        )
        session.commit()

        tracer = tracer or active_tracer()
        with trace_scope(tracer, name=CAMPAIGN_AGENT_SUGGEST, tags=[TAG_AGENT, TAG_CAMPAIGN]):
            run_conversation_boundary(
                {},
                converse,
                call_tool,
                max_turns=DEFAULT_MAX_TURNS,
                run_id=str(run_id),
                trace_id=str(run_id),
            )

        if not saved:
            raise NothingSaved(_nothing_saved_reason(session, run_id))

        opened = sum(open_on_own(session, session.get(Campaign, cid)) for cid in saved)
        run.cost_kes = run_cost_kes(session, llm_client.model, date.today(), 1)
        run.summary = (
            f"saved {len(saved)} drafts covering {len(covered)} of {summary['total']} "
            f"waiting clients; {opened} opened by the agent"
        )
        run.state = "completed"
        run.finished_at = datetime.now(UTC)
        session.commit()
        return run
    except Exception as exc:
        session.rollback()
        run = session.get(CampaignAgentRun, run_id)
        run.state = "failed"
        run.failure_reason = str(exc)
        run.finished_at = datetime.now(UTC)
        session.commit()
        raise


def run_suggestions_in_background(run_id: int) -> None:
    with SessionLocal() as session:
        try:
            execute_suggestion_run(session, run_id, tracer=get_shared_tracer())
        except Exception:
            logger.exception("campaign_suggester.failed", run_id=run_id)
