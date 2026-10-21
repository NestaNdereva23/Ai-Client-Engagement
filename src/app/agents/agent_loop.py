"""The nightly agent loop: gather tonight's groups, plan, choose an action per group,
check every gate, write the proposals, and report."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypedDict

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import card_copy
from app.agents.action_brief import proposal_prohibitions
from app.agents.action_catalog import load_action, selectable_actions
from app.agents.angle_brief import angle_for_proposal
from app.agents.events import NO_EVENTS, EventLog, RunEventLog
from app.agents.fa_scope import current_fa_id, fa_scope
from app.agents.permissions import effective_permission
from app.agents.prompt_versioning import AGENT_LOOP_CHOOSE, AGENT_LOOP_PLAN, active_prompt
from app.agents.proposal_state import transition_proposal
from app.agents.propose import (
    ALREADY_IN_A_RUNNING_PROPOSAL,
    DO_NOTHING_ACTION,
    WATCH_FOR_NOW_ACTION,
    default_action_code_for_group,
    group_evidence,
    group_evidence_detail,
    group_skip_reasons,
    load_action_or_raise,
    member_key,
    skip_reason_counts,
)
from app.agents.run_cost import run_cost_kes
from app.agents.situation_action_mapping import actions_for_situation
from app.agents.situations import (
    DEPOSIT_SIZE_FALLING,
    FREQUENT_DEPOSITOR_ONE_FUND,
    RISK_ROSE_NO_CALL,
)
from app.agents.tool_runtime import make_tool_executor
from app.agents.tools import GROUP_LEVEL_TOOL_SPECS
from app.agents.watchlist import (
    GROUP_TO_SITUATION,
    GroupMember,
    WatchGroup,
    WatchlistThresholds,
    build_watchlist,
    load_thresholds,
)
from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.agent import AgentActionCatalog
from app.db.models.agent_event import (
    ACTION_COMPLETED,
    ACTION_STARTED,
    APPROVAL_GIVEN,
    APPROVAL_NEEDED,
    ERROR,
    PAUSED,
    PROPOSAL_CREATED,
    RUN_COMPLETED,
    RUN_STARTED,
    RUN_STATUS,
    STEP_COMPLETED,
    STEP_STARTED,
    WARNING,
)
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.agent_run import BOOK_WIDE_AGENTS, NIGHTLY_AGENT, AgentRun
from app.db.models.risk import RiskRun
from app.llmops.spans import (
    ModelCallTally,
    counting_converse,
    traced_converse,
    traced_tool_call,
)
from app.llmops.tracing import NullTracer, Tracer
from app.privacy.boundary import AuditSink, run_conversation_boundary
from app.privacy.llm_client import (
    ConversingLLMClient,
    ToolSpec,
    as_converse_call,
    get_agent_llm_client,
)
from app.schemas.agent_choice import AgentChoice

logger = structlog.get_logger(__name__)

DEFAULT_PLAN_MAX_TURNS = 6
DEFAULT_CHOOSE_MAX_TURNS = 6
DEFAULT_MAX_CHOOSE_ATTEMPTS = 2
DEFAULT_MAX_PLAN_ATTEMPTS = 2

FALLBACK_PLAN_TEXT = "The model did not settle on a plan tonight, so no group is treated as urgent."

NOT_SELECTED_TONIGHT = "not_selected_tonight"

CHOOSE_ACTION_TOOL_NAME = "choose_action"

ACTS_ALONE = "act_alone"

PAUSE = "pause"
CANCEL = "cancel"

# These groups always get an outreach attempt: the model may still pick
# which action and angle fits best, but it may not leave the group with
# no message at all. A member is only left out by an actual contact gate
# (suppression, complaint, cooldown, angle paused), never by the model's
# own judgement call to wait. Both fee pressure groups are in this set for
# the same reason: a client whose balance the fee is about to empty needs
# to hear about it, not be skipped because the model thought another
# proposal already covered them.
MUST_REACH_OUT_GROUPS = frozenset(
    {
        DEPOSIT_SIZE_FALLING,
        FREQUENT_DEPOSITOR_ONE_FUND,
        RISK_ROSE_NO_CALL,
    }
)
NO_MESSAGE_ACTIONS = (DO_NOTHING_ACTION, WATCH_FOR_NOW_ACTION)


class AgentRunInProgress(RuntimeError):
    """Raised when a run is asked to start while another one is still running."""


# Unwind a step back to the driver without tripping its error handling.
class _RunPaused(Exception):
    pass


class _RunCancelled(Exception):
    pass


@dataclass(frozen=True)
class GroupDecision:
    """One group's outcome after the check step: the action to propose and who is in or out.
    Every member who is out has a reason."""

    action: AgentActionCatalog
    angle: str | None
    reason: str
    included: tuple[GroupMember, ...]
    skip_reasons: dict[tuple[int, int], str | None]


def _trace_action(action: AgentActionCatalog) -> dict[str, Any]:
    return {"action_code": action.action_code, "title": action.title}


def _trace_group(group: WatchGroup) -> dict[str, Any]:
    return {
        "name": group.name,
        "client_count": group.client_count,
        "fund_count": group.fund_count,
    }


def _trace_decision(decision: GroupDecision) -> dict[str, Any]:
    return {
        "action_code": decision.action.action_code,
        "angle": decision.angle,
        "reason": decision.reason,
        "included_count": len(decision.included),
        "excluded_count": len(decision.skip_reasons) - len(decision.included),
    }


def _trace_proposal(proposal: AgentProposal) -> dict[str, Any]:
    return {
        "proposal_id": proposal.proposal_id,
        "group_name": proposal.group_name,
        "action_code": proposal.action_code,
        "client_count": proposal.client_count,
        "money_total_kes": proposal.money_total_kes,
        "status": proposal.status,
    }


def _trace_value(value: Any) -> Any:
    """A value from AgentLoopState, made safe for a trace.
    Groups and proposals collapse to counts and codes, never a client id."""
    if isinstance(value, AgentActionCatalog):
        return _trace_action(value)
    if isinstance(value, WatchGroup):
        return _trace_group(value)
    if isinstance(value, GroupDecision):
        return _trace_decision(value)
    if isinstance(value, AgentProposal):
        return _trace_proposal(value)
    if isinstance(value, AgentChoice):
        return value.model_dump()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _trace_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_trace_value(item) for item in value]
    return value


_GROUP_LEVEL_TOOL_NAMES = frozenset(spec.name for spec in GROUP_LEVEL_TOOL_SPECS)


def _is_unmade_tool_call(text: str, tool_names: frozenset[str]) -> bool:
    """True when text is a tool call the model wrote out instead of making,
    e.g. the literal string {"list_groups": {}}."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return False
    return isinstance(parsed, dict) and bool(parsed) and set(parsed) <= tool_names


def _span_input(state: AgentLoopState) -> dict[str, Any]:
    return {key: _trace_value(value) for key, value in state.items()}


def _span_output(result: dict[str, Any]) -> dict[str, Any]:
    return {key: _trace_value(value) for key, value in result.items()}


class AgentLoopState(TypedDict, total=False):
    """State threaded through the six steps of one nightly run."""

    groups: Sequence[WatchGroup]
    actions: dict[str, AgentActionCatalog]
    thresholds: WatchlistThresholds
    plan_text: str
    choices: dict[str, AgentChoice]
    checked: dict[str, GroupDecision]
    proposals: list[AgentProposal]
    summary: str
    cost_kes: float | None
    model_call_count: int
    input_tokens: int
    output_tokens: int


class MissingPromptTemplate(RuntimeError):
    pass


def build_plan_system_prompt(session: Session, as_of: date) -> tuple[str, int]:
    row = active_prompt(session, AGENT_LOOP_PLAN, as_of)
    if row is None:
        raise MissingPromptTemplate(f"no published '{AGENT_LOOP_PLAN}' prompt as of {as_of}")
    return row.template.format(as_of=as_of.isoformat()), row.version


def build_choose_system_prompt(
    session: Session,
    *,
    actions: Mapping[str, AgentActionCatalog],
    candidate_group_names: Sequence[str],
    plan_text: str,
    as_of: date,
) -> tuple[str, int]:
    menu = "\n".join(
        f"- {code}: {row.title}. Who it is for: {row.who}. "
        + (f"Uses the angle '{row.message_angle}'." if row.message_angle else "Sends nothing.")
        for code, row in sorted(actions.items())
    )
    groups = ", ".join(sorted(candidate_group_names))
    row = active_prompt(session, AGENT_LOOP_CHOOSE, as_of)
    if row is None:
        raise MissingPromptTemplate(f"no published '{AGENT_LOOP_CHOOSE}' prompt as of {as_of}")
    prompt = row.template.format(
        as_of=as_of.isoformat(),
        plan_text=plan_text,
        groups=groups,
        menu=menu,
        tool_name=CHOOSE_ACTION_TOOL_NAME,
    )
    return prompt, row.version


def build_choose_action_tool_spec(
    *, actions: Mapping[str, AgentActionCatalog], candidate_group_names: Sequence[str]
) -> ToolSpec:
    """The tool the choose step's model calls to record one group's decision.
    Offered only during choose, so there is no reply text left to parse."""
    return ToolSpec(
        name=CHOOSE_ACTION_TOOL_NAME,
        description=(
            "Record tonight's decision for one group: an action code from the "
            "menu, or 'do_nothing' to leave the group alone. Call this once for "
            "every group you were given, then reply with plain text and no "
            "further tool call. Calling it again for a group you already "
            "decided replaces that decision."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "group_name": {
                    "type": "string",
                    "description": "One of tonight's candidate groups.",
                    "enum": sorted(candidate_group_names),
                },
                "action_code": {
                    "type": "string",
                    "description": "One action code from tonight's menu, written exactly as shown.",
                    "enum": sorted(actions),
                },
                "angle": {
                    "type": ["string", "null"],
                    "description": "The action's exact angle, or null when it sends nothing.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this group matters tonight, in plain everyday words.",
                },
            },
            "required": ["group_name", "action_code", "reason"],
        },
    )


def make_choose_action_tool(
    *,
    candidate_group_names: Sequence[str],
    actions: Mapping[str, AgentActionCatalog],
    group_to_allowed_action_codes: Mapping[str, frozenset[str]],
    choices: dict[str, AgentChoice],
    refusals: list[str],
) -> Callable[..., dict[str, Any]]:
    """Build the choose_action tool function for one attempt at the choose step.
    Choices and refusals are written into the lists passed in; a bad call returns an error dict."""
    valid_names = set(candidate_group_names)

    def choose_action(
        session: Session,
        *,
        group_name: str,
        action_code: str,
        reason: str,
        angle: str | None = None,
    ) -> dict[str, Any]:
        if group_name not in valid_names:
            message = f"'{group_name}' is not one of tonight's candidate groups"
            refusals.append(message)
            return {"error": "unknown_group", "message": message}

        action = actions.get(action_code)
        if action is None:
            message = f"'{action_code}' is not on tonight's list of actions"
            refusals.append(message)
            return {"error": "unknown_action", "message": message}

        allowed = group_to_allowed_action_codes.get(group_name, frozenset())
        if action_code not in NO_MESSAGE_ACTIONS and action_code not in allowed:
            message = f"'{action_code}' is not mapped to '{group_name}' tonight"
            refusals.append(message)
            return {"error": "wrong_action", "message": message}

        if angle != action.message_angle:
            message = f"'{action_code}' takes the angle {action.message_angle!r}, not {angle!r}"
            refusals.append(message)
            return {"error": "wrong_angle", "message": message}

        if not reason.strip():
            message = "give a short reason for this group"
            refusals.append(message)
            return {"error": "missing_reason", "message": message}

        choices[group_name] = AgentChoice(
            group_name=group_name, action_code=action_code, angle=angle, reason=reason
        )
        return {
            "status": "recorded",
            "group_name": group_name,
            "action_code": action_code,
            "angle": angle,
        }

    return choose_action


def build_agent_loop_steps(
    *,
    session: Session,
    run_id: int,
    trace_id: str,
    as_of: date,
    llm_client: ConversingLLMClient,
    cooldown_days: int | None = None,
    max_choose_attempts: int = DEFAULT_MAX_CHOOSE_ATTEMPTS,
    tracer: Tracer | None = None,
    audit: AuditSink | None = None,
    events: EventLog = NO_EVENTS,
) -> list[tuple[str, Callable[[AgentLoopState], dict[str, Any]]]]:
    """The six steps, named and traced, in the order a run takes them."""
    tracer = tracer or NullTracer()

    def _traced(name: str, fn):
        """One span per step, handed to the step so it can nest its own work."""

        def wrapped(state: AgentLoopState) -> dict[str, Any]:
            handle = tracer.start_span(
                trace_id=trace_id,
                name=name,
                input=_span_input(state),
                metadata={"run_id": run_id, "as_of": as_of.isoformat()},
            )
            events.record(STEP_STARTED, step=name)
            try:
                result = fn(state, handle)
            except (_RunPaused, _RunCancelled):
                tracer.end_span(handle, output={"stopped": "pause_or_cancel"})
                raise
            except Exception as exc:
                tracer.end_span(
                    handle, output={"error": str(exc)}, level="ERROR", status_message=str(exc)
                )
                events.record(ERROR, about="step", step=name, reason=str(exc))
                raise
            tracer.end_span(handle, output=_span_output(result))
            events.record(STEP_COMPLETED, step=name)
            return result

        return wrapped

    def gather(state: AgentLoopState, _span: Any) -> dict[str, Any]:
        thresholds = load_thresholds(session, as_of)
        groups = build_watchlist(session, as_of, thresholds)
        actions = selectable_actions(session, as_of)

        logger.info(
            "agent_loop.gather",
            run_id=run_id,
            as_of=as_of.isoformat(),
            groups_with_members=[g.name for g in groups if g.members],
            group_sizes={g.name: len(g.members) for g in groups},
            selectable_actions=sorted(actions),
        )
        record_audit(
            session,
            entity_type="agent_run",
            action="gather",
            entity_id=str(run_id),
            run_id=str(run_id),
            detail={
                "as_of": as_of.isoformat(),
                "groups_with_members": [g.name for g in groups if g.members],
                "selectable_actions": sorted(actions),
            },
        )
        session.commit()
        events.record(
            RUN_STATUS,
            stage="gathered",
            group_count=len([g for g in groups if g.members]),
            action_count=len(actions),
        )
        return {"groups": groups, "actions": actions, "thresholds": thresholds}

    def plan(state: AgentLoopState, span: Any) -> dict[str, Any]:
        resumed_row = session.get(AgentRun, run_id)
        if resumed_row.plan_text is not None:
            return {"plan_text": resumed_row.plan_text}

        system_prompt, prompt_version = build_plan_system_prompt(session, as_of)
        tally = ModelCallTally()
        call_tool = traced_tool_call(
            make_tool_executor(session, run_id, events=events),
            tracer=tracer,
            trace_id=trace_id,
            parent=span,
        )
        converse = traced_converse(
            counting_converse(
                as_converse_call(llm_client, system=system_prompt, tools=GROUP_LEVEL_TOOL_SPECS),
                tally,
            ),
            tracer=tracer,
            trace_id=trace_id,
            model=llm_client.model,
            parent=span,
            system=system_prompt,
        )

        # A weak model occasionally echoes a tool call as plain text instead
        # of actually calling the tool, e.g. the reply is the literal string
        # {"list_groups": {}}. That is never a real plan, so give it one more
        # try before falling back.
        result = None
        plan_text = ""
        for _ in range(DEFAULT_MAX_PLAN_ATTEMPTS):
            result = run_conversation_boundary(
                {},
                converse,
                call_tool,
                max_turns=DEFAULT_PLAN_MAX_TURNS,
                run_id=str(run_id),
                trace_id=trace_id,
                audit=audit,
            )
            plan_text = (result.final_text or "").strip()
            if result.stopped_reason != "final_answer" or not _is_unmade_tool_call(
                plan_text, _GROUP_LEVEL_TOOL_NAMES
            ):
                break

        if (
            result.stopped_reason != "final_answer"
            or not plan_text
            or _is_unmade_tool_call(plan_text, _GROUP_LEVEL_TOOL_NAMES)
        ):
            plan_text = FALLBACK_PLAN_TEXT

        logger.info(
            "agent_loop.plan",
            run_id=run_id,
            stopped_reason=result.stopped_reason,
            plan_text=plan_text,
            **tally.as_detail(),
        )
        record_audit(
            session,
            entity_type="agent_run",
            action="plan",
            entity_id=str(run_id),
            run_id=str(run_id),
            detail={
                "plan_text": plan_text,
                "stopped_reason": result.stopped_reason,
                **tally.as_detail(),
            },
        )
        run_row = session.get(AgentRun, run_id)
        run_row.plan_text = plan_text
        run_row.prompt_versions = {
            **(run_row.prompt_versions or {}),
            AGENT_LOOP_PLAN: prompt_version,
        }
        session.commit()
        return {
            "plan_text": plan_text,
            "model_call_count": tally.calls,
            "input_tokens": tally.input_tokens,
            "output_tokens": tally.output_tokens,
        }

    def choose(state: AgentLoopState, span: Any) -> dict[str, Any]:
        resumed_row = session.get(AgentRun, run_id)
        if resumed_row.choices is not None:
            return {
                "choices": {
                    name: AgentChoice.model_validate(value)
                    for name, value in resumed_row.choices.items()
                }
            }

        groups = state["groups"]
        actions = state["actions"]
        candidate_names = [g.name for g in groups if g.members]
        group_to_allowed_action_codes = {
            name: frozenset(
                row.action_code
                for row in actions_for_situation(session, GROUP_TO_SITUATION.get(name, name), as_of)
            )
            for name in candidate_names
        }

        choices: dict[str, AgentChoice] = {}
        fell_back = False
        last_error: str | None = None
        attempts = 0
        prompt_version: int | None = None
        tally = ModelCallTally()

        if candidate_names:
            fell_back = True
            system_prompt, prompt_version = build_choose_system_prompt(
                session,
                actions=actions,
                candidate_group_names=candidate_names,
                plan_text=state["plan_text"],
                as_of=as_of,
            )
            tools = (
                *GROUP_LEVEL_TOOL_SPECS,
                build_choose_action_tool_spec(
                    actions=actions, candidate_group_names=candidate_names
                ),
            )
            tool_names = frozenset(spec.name for spec in tools)
            converse = traced_converse(
                counting_converse(
                    as_converse_call(llm_client, system=system_prompt, tools=tools), tally
                ),
                tracer=tracer,
                trace_id=trace_id,
                model=llm_client.model,
                parent=span,
                system=system_prompt,
            )

            for _ in range(max_choose_attempts):
                attempt_choices: dict[str, AgentChoice] = {}
                refusals: list[str] = []
                choose_action_tool = make_choose_action_tool(
                    candidate_group_names=candidate_names,
                    actions=actions,
                    group_to_allowed_action_codes=group_to_allowed_action_codes,
                    choices=attempt_choices,
                    refusals=refusals,
                )
                call_tool = traced_tool_call(
                    make_tool_executor(
                        session,
                        run_id,
                        extra_tools={CHOOSE_ACTION_TOOL_NAME: choose_action_tool},
                        events=events,
                    ),
                    tracer=tracer,
                    trace_id=trace_id,
                    parent=span,
                )

                result = run_conversation_boundary(
                    {},
                    converse,
                    call_tool,
                    max_turns=DEFAULT_CHOOSE_MAX_TURNS,
                    run_id=str(run_id),
                    trace_id=trace_id,
                    audit=audit,
                )
                attempts += 1
                if result.stopped_reason != "final_answer":
                    last_error = (
                        refusals[-1]
                        if refusals
                        else "the model did not settle on a final answer before the turn cap"
                    )
                    continue
                if not attempt_choices and _is_unmade_tool_call(
                    (result.final_text or "").strip(), tool_names
                ):
                    last_error = "the model wrote a tool call instead of making one"

                    continue
                choices = attempt_choices
                fell_back = False
                last_error = None
                break

        logger.info(
            "agent_loop.choose",
            run_id=run_id,
            chosen={
                name: {"action_code": choice.action_code, "angle": choice.angle}
                for name, choice in choices.items()
            },
            fell_back=fell_back,
            attempts=attempts,
            last_error=last_error,
            **tally.as_detail(),
        )
        record_audit(
            session,
            entity_type="agent_run",
            action="choose",
            entity_id=str(run_id),
            run_id=str(run_id),
            detail={
                "chosen": {
                    name: {"action_code": choice.action_code, "angle": choice.angle}
                    for name, choice in choices.items()
                },
                "fell_back": fell_back,
                "attempts": attempts,
                "last_error": last_error,
                **tally.as_detail(),
            },
        )
        run_row = session.get(AgentRun, run_id)
        run_row.choices = {name: choice.model_dump() for name, choice in choices.items()}
        if prompt_version is not None:
            run_row.prompt_versions = {
                **(run_row.prompt_versions or {}),
                AGENT_LOOP_CHOOSE: prompt_version,
            }
        session.commit()
        events.record(
            RUN_STATUS,
            stage="chosen",
            groups_needing_a_decision=len(candidate_names),
            chosen_count=len(choices),
            attempts=attempts,
        )
        if fell_back:
            events.record(WARNING, about="choose", reason="fell_back", last_error=last_error)
        return {
            "choices": choices,
            "model_call_count": state.get("model_call_count", 0) + tally.calls,
            "input_tokens": state.get("input_tokens", 0) + tally.input_tokens,
            "output_tokens": state.get("output_tokens", 0) + tally.output_tokens,
        }

    def check(state: AgentLoopState, _span: Any) -> dict[str, Any]:
        checked: dict[str, GroupDecision] = {}
        for group in state["groups"]:
            if not group.members:
                continue
            choice = state["choices"].get(group.name)
            chosen_code = choice.action_code if choice is not None else None

            overridden = False
            if group.name in MUST_REACH_OUT_GROUPS and (
                chosen_code is None or chosen_code in NO_MESSAGE_ACTIONS
            ):
                fallback_code = default_action_code_for_group(session, group.name, as_of)
                if (
                    fallback_code is not None
                    and fallback_code not in NO_MESSAGE_ACTIONS
                    and load_action(session, fallback_code, as_of) is not None
                ):
                    chosen_code = fallback_code
                    overridden = True

            if chosen_code is None or (chosen_code == DO_NOTHING_ACTION and not overridden):
                action = load_action_or_raise(session, DO_NOTHING_ACTION, as_of)
                reason = (
                    choice.reason
                    if choice is not None
                    else "the AI Assistant did not choose an action for this group tonight"
                )
                checked[group.name] = GroupDecision(
                    action=action,
                    angle=None,
                    reason=reason,
                    included=(),
                    skip_reasons={
                        member_key(member): NOT_SELECTED_TONIGHT for member in group.members
                    },
                )
                continue

            action = load_action_or_raise(session, chosen_code, as_of)
            angle = action.message_angle if overridden else choice.angle
            reason = (
                f"{action.title} is this group's required outreach action; the AI Assistant "
                "chose not to send anything tonight, so this fixed default was used instead."
                if overridden
                else choice.reason
            )
            skip_reasons = group_skip_reasons(
                session, group.name, group.members, action, as_of, cooldown_days
            )
            included = tuple(
                member for member in group.members if skip_reasons[member_key(member)] is None
            )
            if not included:
                action = load_action_or_raise(session, DO_NOTHING_ACTION, as_of)
                angle = None

            checked[group.name] = GroupDecision(
                action=action,
                angle=angle,
                reason=reason,
                included=included,
                skip_reasons=skip_reasons,
            )

        logger.info(
            "agent_loop.check",
            run_id=run_id,
            decisions={
                name: {
                    "action_code": decision.action.action_code,
                    "included_count": len(decision.included),
                    "excluded_count": len(decision.skip_reasons) - len(decision.included),
                }
                for name, decision in checked.items()
            },
        )
        record_audit(
            session,
            entity_type="agent_run",
            action="check",
            entity_id=str(run_id),
            run_id=str(run_id),
            detail={
                name: {
                    "action_code": decision.action.action_code,
                    "included_count": len(decision.included),
                    "excluded_count": len(decision.skip_reasons) - len(decision.included),
                }
                for name, decision in checked.items()
            },
        )
        session.commit()
        already_running_count = sum(
            1
            for decision in checked.values()
            for reason in decision.skip_reasons.values()
            if reason == ALREADY_IN_A_RUNNING_PROPOSAL
        )
        events.record(
            RUN_STATUS,
            stage="checked",
            group_count=len(checked),
            already_running_count=already_running_count,
        )
        return {"checked": checked}

    def propose(state: AgentLoopState, span: Any) -> dict[str, Any]:
        proposals: list[AgentProposal] = []
        decided_groups = [g for g in state["groups"] if state["checked"].get(g.name) is not None]
        for index, group in enumerate(decided_groups):
            decision = state["checked"][group.name]

            # A resumed run already committed some groups; show them again, don't redo them.
            existing = session.scalar(
                select(AgentProposal).where(
                    AgentProposal.run_id == run_id, AgentProposal.group_name == group.name
                )
            )
            if existing is not None:
                proposals.append(existing)
                continue

            money_total_kes = sum(member.balance for member in decision.included)
            evidence = group_evidence(group, state["thresholds"])
            evidence_detail = group_evidence_detail(session, group)
            angle = angle_for_proposal(
                session,
                action=decision.action,
                group_name=group.name,
                client_count=group.client_count,
                money_total_kes=money_total_kes,
                evidence=evidence,
                evidence_detail=evidence_detail,
                run_id=run_id,
                as_of=as_of,
                llm_client=llm_client,
                audit=audit,
                tracer=tracer,
                trace_id=trace_id,
                parent=span,
            )
            proposal = AgentProposal(
                run_id=run_id,
                fa_id=current_fa_id(),
                action_code=decision.action.action_code,
                catalog_version=decision.action.version,
                group_name=group.name,
                group_definition=dict(group.definition),
                client_count=group.client_count,
                money_total_kes=money_total_kes,
                evidence=evidence,
                evidence_detail=evidence_detail,
                reason=decision.reason,
                angle=angle,
                content_mix=decision.action.content_mix,
                response_kind=decision.action.response_kind,
                permission_applied=effective_permission(
                    session,
                    decision.action.action_code,
                    money_total_kes=money_total_kes,
                    money_ceiling_kes=decision.action.money_ceiling_kes,
                    at=as_of,
                ),
                skip_reason_counts=skip_reason_counts(decision.skip_reasons),
                status="proposed",
            )
            session.add(proposal)
            session.flush()

            session.add_all(
                AgentProposalClient(
                    proposal_id=proposal.proposal_id,
                    client_id=member.client_id,
                    unit_fund_id=member.unit_fund_id,
                    included=decision.skip_reasons[member_key(member)] is None,
                    skip_reason=decision.skip_reasons[member_key(member)],
                )
                for member in group.members
            )
            session.flush()

            logger.info(
                "agent_loop.propose",
                run_id=run_id,
                proposal_id=proposal.proposal_id,
                group_name=group.name,
                action_code=decision.action.action_code,
                included_count=len(decision.included),
                money_total_kes=proposal.money_total_kes,
            )
            record_audit(
                session,
                entity_type="agent_proposal",
                action="create",
                entity_id=str(proposal.proposal_id),
                run_id=str(run_id),
                detail={
                    "group_name": group.name,
                    "action_code": decision.action.action_code,
                    "included_count": len(decision.included),
                    "angle": proposal.angle,
                },
            )
            session.commit()
            copy = card_copy.copy_for_history(group.name)
            events.record(
                PROPOSAL_CREATED,
                proposal_id=proposal.proposal_id,
                group_name=group.name,
                action_code=decision.action.action_code,
                client_count=proposal.client_count,
                included_count=len(decision.included),
                money_total_kes=money_total_kes,
                skip_reason_counts=proposal.skip_reason_counts,
                permission_applied=proposal.permission_applied,
                card_title=copy.card_title,
                screen_label=copy.screen_label,
                why_now=copy.why_now,
                suggested_owner=card_copy.suggested_owner_for(decision.action.response_kind),
            )
            if decision.action.action_code != DO_NOTHING_ACTION:
                if proposal.permission_applied == ACTS_ALONE:
                    proposal = transition_proposal(
                        session,
                        proposal,
                        to_status="approved",
                        reason="act_alone permission needs no human approval",
                        decided_by="agent",
                    )
                    session.commit()
                    events.record(
                        APPROVAL_GIVEN,
                        proposal_id=proposal.proposal_id,
                        group_name=group.name,
                        action_code=decision.action.action_code,
                        decided_by="agent",
                    )

                    from app.agents.write_tools import run_proposal as _run_proposal_tool

                    events.record(
                        ACTION_STARTED,
                        proposal_id=proposal.proposal_id,
                        group_name=group.name,
                        action_code=decision.action.action_code,
                    )
                    try:
                        run_result = _run_proposal_tool(
                            session,
                            proposal.proposal_id,
                            run_id=run_id,
                            as_of=as_of,
                            cooldown_days=cooldown_days,
                            tracer=tracer,
                        )
                    except Exception as exc:
                        session.rollback()
                        proposal = session.get(AgentProposal, proposal.proposal_id)
                        run_result = {"error": "run_failed", "message": str(exc)}
                        events.record(
                            ERROR,
                            about="action",
                            step="propose",
                            reason=str(exc),
                            proposal_id=proposal.proposal_id,
                        )
                    events.record(
                        ACTION_COMPLETED,
                        proposal_id=proposal.proposal_id,
                        group_name=group.name,
                        action_code=decision.action.action_code,
                        result=run_result,
                    )
                else:
                    events.record(
                        APPROVAL_NEEDED,
                        proposal_id=proposal.proposal_id,
                        group_name=group.name,
                        action_code=decision.action.action_code,
                        included_count=len(decision.included),
                    )
            proposals.append(proposal)

            if index < len(decided_groups) - 1:
                _wait_between_groups(
                    session, run_id, events, next_group=decided_groups[index + 1].name
                )

        record_audit(
            session,
            entity_type="agent_run",
            action="propose",
            entity_id=str(run_id),
            run_id=str(run_id),
            detail={"proposal_count": len(proposals)},
        )
        session.commit()
        return {"proposals": proposals}

    def report(state: AgentLoopState, _span: Any) -> dict[str, Any]:
        proposals = state["proposals"]
        checked = state["checked"]

        acted = [p for p in proposals if p.action_code != DO_NOTHING_ACTION]
        included_total = sum(len(checked[p.group_name].included) for p in proposals)
        excluded_total = sum(
            len(decision.skip_reasons) - len(decision.included) for decision in checked.values()
        )
        empty_groups = len(state["groups"]) - len(proposals)

        lines = []
        if acted:
            action_word = "action" if len(acted) == 1 else "actions"
            client_word = "client" if included_total == 1 else "clients"
            lines.append(
                f"{len(acted)} {action_word} proposed, covering {included_total} {client_word}."
            )
        else:
            lines.append("Nothing was proposed tonight.")
        if excluded_total:
            lines.append(f"{excluded_total} clients were left out by a check.")
        if empty_groups:
            lines.append(f"{empty_groups} groups had nobody in them tonight.")

        cost_kes = run_cost_kes(session, llm_client.model, as_of, state.get("model_call_count", 0))
        if cost_kes is not None:
            lines.append(f"This run cost about {cost_kes:.2f} KES in model calls.")

        summary = " ".join(lines)
        logger.info("agent_loop.report", run_id=run_id, summary=summary, cost_kes=cost_kes)
        record_audit(
            session,
            entity_type="agent_run",
            action="report",
            entity_id=str(run_id),
            run_id=str(run_id),
            detail={
                "summary": summary,
                "cost_kes": cost_kes,
                "model_calls": state.get("model_call_count", 0),
                "input_tokens": state.get("input_tokens", 0),
                "output_tokens": state.get("output_tokens", 0),
                "trace_id": trace_id,
                "trace_url": tracer.get_trace_url(trace_id),
            },
        )
        run_row = session.get(AgentRun, run_id)
        run_row.summary = summary
        run_row.cost_kes = cost_kes
        session.commit()
        return {"summary": summary, "cost_kes": cost_kes}

    return [
        ("gather", _traced("gather", gather)),
        ("plan", _traced("plan", plan)),
        ("choose", _traced("choose", choose)),
        ("check", _traced("check", check)),
        ("propose", _traced("propose", propose)),
        ("report", _traced("report", report)),
    ]


def _control_requested(session: Session, run_id: int) -> str | None:
    return session.scalar(select(AgentRun.control_requested).where(AgentRun.run_id == run_id))


def _wait_between_groups(
    session: Session, run_id: int, events: EventLog, *, next_group: str
) -> None:
    window = get_settings().agent_group_pause_window_seconds
    waited = 0
    while waited < window:
        control = _control_requested(session, run_id)
        if control == PAUSE:
            raise _RunPaused()
        if control == CANCEL:
            raise _RunCancelled()
        events.record(
            RUN_STATUS, stage="group_wait", seconds_left=window - waited, next_group=next_group
        )
        time.sleep(1)
        waited += 1


def _settle_paused(session: Session, run_id: int, events: EventLog) -> None:
    session.rollback()
    run = session.get(AgentRun, run_id)
    run.state = "paused"
    run.control_requested = None
    record_audit(
        session, entity_type="agent_run", action="paused", entity_id=str(run_id), run_id=str(run_id)
    )
    session.commit()
    events.record(PAUSED)
    logger.info("agent_loop.run_paused", run_id=run_id)


def _settle_cancelled(session: Session, run_id: int, events: EventLog) -> None:
    session.rollback()
    run = session.get(AgentRun, run_id)
    run.state = "cancelled"
    run.control_requested = None
    run.finished_at = datetime.now(UTC)
    record_audit(
        session,
        entity_type="agent_run",
        action="cancelled",
        entity_id=str(run_id),
        run_id=str(run_id),
    )
    session.commit()
    events.record(RUN_COMPLETED, state="cancelled")
    logger.info("agent_loop.run_cancelled", run_id=run_id)


def _run_steps(
    steps: list[tuple[str, Callable[[AgentLoopState], dict[str, Any]]]],
    *,
    session: Session,
    run_id: int,
    events: EventLog,
) -> str:
    state: AgentLoopState = {}
    for _name, fn in steps:
        control = _control_requested(session, run_id)
        if control == PAUSE:
            _settle_paused(session, run_id, events)
            return "paused"
        if control == CANCEL:
            _settle_cancelled(session, run_id, events)
            return "cancelled"
        try:
            state.update(fn(state))
        except _RunPaused:
            _settle_paused(session, run_id, events)
            return "paused"
        except _RunCancelled:
            _settle_cancelled(session, run_id, events)
            return "cancelled"
    return "completed"


def _latest_completed_risk_run_id(session: Session) -> str | None:
    """The most recently finished risk run, or None if there has never been one.
    A record of which run was on file, not a dependency."""
    return session.scalar(
        select(RiskRun.run_id)
        .where(RiskRun.state == "completed")
        .order_by(RiskRun.finished_at.desc())
        .limit(1)
    )


def fail_stale_runs(
    session: Session, *, max_runtime_minutes: int, now: datetime | None = None
) -> list[int]:
    cutoff = (now or datetime.now(UTC)) - timedelta(minutes=max_runtime_minutes)
    stale = session.scalars(
        select(AgentRun).where(AgentRun.state == "running", AgentRun.started_at < cutoff)
    ).all()
    if not stale:
        return []
    for run in stale:
        run.state = "failed"
        run.finished_at = datetime.now(UTC)
        run.failure_reason = "run exceeded the maximum runtime and was treated as orphaned"
        record_audit(
            session,
            entity_type="agent_run",
            action="failed",
            entity_id=str(run.run_id),
            run_id=str(run.run_id),
            detail={"reason": "stale", "max_runtime_minutes": max_runtime_minutes},
        )
    session.commit()
    run_ids = [run.run_id for run in stale]
    logger.info("agent_loop.stale_runs_failed", run_ids=run_ids, count=len(run_ids))
    return run_ids


def start_agent_run(
    session: Session,
    *,
    trigger: str,
    as_of: date | None = None,
    kind: str = NIGHTLY_AGENT,
    insight_id: int | None = None,
    fa_id: str | None = None,
) -> AgentRun:
    """Open a new agent_run row and commit it, ready for execute_agent_run.
    A whole book run refuses with AgentRunInProgress when another is under way."""
    as_of = as_of or date.today()
    fail_stale_runs(session, max_runtime_minutes=get_settings().agent_run_stale_after_minutes)
    if kind in BOOK_WIDE_AGENTS:
        in_progress = session.scalar(
            select(AgentRun)
            .where(
                AgentRun.state == "running",
                AgentRun.agent_kind.in_(BOOK_WIDE_AGENTS),
                AgentRun.fa_id.is_(None) if fa_id is None else AgentRun.fa_id == fa_id,
            )
            .limit(1)
        )
        if in_progress is not None:
            raise AgentRunInProgress(f"agent run {in_progress.run_id} is already running")

    risk_run_id = _latest_completed_risk_run_id(session)
    run = AgentRun(
        trigger=trigger,
        risk_run_id=risk_run_id,
        agent_kind=kind,
        insight_id=insight_id,
        fa_id=fa_id,
    )
    session.add(run)
    session.commit()
    logger.info(
        "agent_loop.run_started",
        run_id=run.run_id,
        trigger=trigger,
        agent_kind=kind,
        as_of=as_of.isoformat(),
        risk_run_id=risk_run_id,
    )
    return run


def _resume_running_campaigns(session: Session, *, events: EventLog) -> dict[str, int]:
    """Draft the next batch for campaigns an earlier proposal already started.
    Runs before tonight's proposing, so last night's leftovers go out first."""
    proposals = session.scalars(
        select(AgentProposal).where(
            AgentProposal.status == "running",
            AgentProposal.campaign_id.is_not(None),
        )
    ).all()

    from app.agents.write_tools import draft_into_review_queue

    campaigns_drafted = 0
    drafted_total = 0
    for proposal in proposals:
        try:
            drafted = draft_into_review_queue(
                session,
                campaign_id=proposal.campaign_id,
                prohibitions=proposal_prohibitions(session, proposal),
            )
        except Exception as exc:
            logger.exception(
                "agent_loop.resume_campaign_failed",
                proposal_id=proposal.proposal_id,
                campaign_id=proposal.campaign_id,
            )
            events.record(
                ERROR,
                about="resume_campaigns",
                proposal_id=proposal.proposal_id,
                campaign_id=proposal.campaign_id,
                reason=str(exc),
            )
            continue
        if drafted:
            campaigns_drafted += 1
            drafted_total += drafted
            logger.info(
                "agent_loop.resume_campaign",
                proposal_id=proposal.proposal_id,
                campaign_id=proposal.campaign_id,
                drafted_count=drafted,
            )
    return {
        "campaigns_checked": len(proposals),
        "campaigns_drafted": campaigns_drafted,
        "drafted_count": drafted_total,
    }


def execute_agent_run(
    session: Session,
    run: AgentRun,
    *,
    llm_client: ConversingLLMClient | None = None,
    as_of: date | None = None,
    cooldown_days: int | None = None,
    max_choose_attempts: int = DEFAULT_MAX_CHOOSE_ATTEMPTS,
    tracer: Tracer | None = None,
    audit: AuditSink | None = None,
    events: EventLog | None = None,
) -> AgentRun:
    """Run the six steps against an agent_run row start_agent_run opened.
    Always returns the row; a step that raises leaves the run failed with the reason recorded.
    Also used to resume a paused run: it re-enters the same steps, skipping what is already done."""
    llm_client = llm_client or get_agent_llm_client()
    as_of = as_of or date.today()
    tracer = tracer or NullTracer()
    run_id = run.run_id
    trace_id = uuid.uuid4().hex
    own_events = events is None
    events = RunEventLog(run_id) if own_events else events

    logger.info(
        "agent_loop.run_executing",
        run_id=run_id,
        trace_id=trace_id,
        as_of=as_of.isoformat(),
        model=llm_client.model,
    )
    events.record(
        RUN_STARTED,
        agent="nightly",
        trigger=run.trigger,
        as_of=as_of.isoformat(),
        model=llm_client.model,
    )

    steps = build_agent_loop_steps(
        session=session,
        run_id=run_id,
        trace_id=trace_id,
        as_of=as_of,
        llm_client=llm_client,
        cooldown_days=cooldown_days,
        max_choose_attempts=max_choose_attempts,
        tracer=tracer,
        audit=audit,
        events=events,
    )

    try:
        try:
            if get_settings().agent_resume_campaigns_enabled:
                events.record(STEP_STARTED, step="resume_campaigns")
                with fa_scope(run.fa_id):
                    resume_summary = _resume_running_campaigns(session, events=events)
                events.record(STEP_COMPLETED, step="resume_campaigns", **resume_summary)
            else:
                logger.info("agent_loop.resume_campaigns_skipped", run_id=run_id)

            with fa_scope(run.fa_id):
                outcome = _run_steps(steps, session=session, run_id=run_id, events=events)
        except Exception as exc:
            logger.exception("agent_loop.run_failed", run_id=run_id, reason=str(exc))
            events.record(ERROR, about="run", reason=str(exc))
            events.record(RUN_COMPLETED, state="failed", reason=str(exc))
            session.rollback()
            run = session.get(AgentRun, run_id)
            run.state = "failed"
            run.failure_reason = str(exc)
            run.finished_at = datetime.now(UTC)
            record_audit(
                session,
                entity_type="agent_run",
                action="failed",
                entity_id=str(run_id),
                run_id=str(run_id),
                detail={"reason": str(exc)},
            )
            session.commit()
            tracer.flush()
            return run

        if outcome != "completed":
            tracer.flush()
            return session.get(AgentRun, run_id)

        run = session.get(AgentRun, run_id)
        run.state = "completed"
        run.finished_at = datetime.now(UTC)
        session.commit()
        tracer.flush()
        events.record(RUN_COMPLETED, state="completed", cost_kes=run.cost_kes)
        logger.info(
            "agent_loop.run_completed",
            run_id=run_id,
            summary=run.summary,
            trace_url=tracer.get_trace_url(trace_id),
        )
        return run
    finally:
        if own_events:
            events.close()


def run_nightly_agent(
    session: Session,
    *,
    trigger: str,
    llm_client: ConversingLLMClient | None = None,
    as_of: date | None = None,
    cooldown_days: int | None = None,
    max_choose_attempts: int = DEFAULT_MAX_CHOOSE_ATTEMPTS,
    tracer: Tracer | None = None,
    audit: AuditSink | None = None,
    events: EventLog | None = None,
    fa_id: str | None = None,
) -> AgentRun:
    """Start a run and take it all the way through, in one call.
    For a caller that can wait; the API calls start_agent_run and execute_agent_run apart."""
    run = start_agent_run(session, trigger=trigger, as_of=as_of, fa_id=fa_id)
    return execute_agent_run(
        session,
        run,
        llm_client=llm_client,
        as_of=as_of,
        cooldown_days=cooldown_days,
        max_choose_attempts=max_choose_attempts,
        tracer=tracer,
        audit=audit,
        events=events,
    )
