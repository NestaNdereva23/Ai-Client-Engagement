"""The nightly agent loop: one graph, run once a night and on demand, that
gathers tonight's groups, plans in plain words, chooses an action for the
groups that matter, checks every gate, writes the proposals, and reports
what happened.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.action_catalog import selectable_actions
from app.agents.events import NO_EVENTS, EventLog, RunEventLog
from app.agents.permissions import effective_permission
from app.agents.propose import (
    DO_NOTHING_ACTION,
    group_evidence,
    group_skip_reasons,
    load_action_or_raise,
    member_key,
    skip_reason_counts,
)
from app.agents.run_cost import run_cost_kes
from app.agents.tool_runtime import make_tool_executor
from app.agents.tools import TOOL_SPECS
from app.agents.watchlist import (
    GroupMember,
    WatchGroup,
    WatchlistThresholds,
    build_watchlist,
    load_thresholds,
)
from app.audit.log import record_audit
from app.db.models.agent import AgentActionCatalog
from app.db.models.agent_event import (
    APPROVAL_NEEDED,
    ERROR,
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

FALLBACK_PLAN_TEXT = "The model did not settle on a plan tonight, so no group is treated as urgent."

NOT_SELECTED_TONIGHT = "not_selected_tonight"

CHOOSE_ACTION_TOOL_NAME = "choose_action"

ACTS_ALONE = "act_alone"


class AgentRunInProgress(RuntimeError):
    """Raised when a run is asked to start while another one is still running."""


@dataclass(frozen=True)
class GroupDecision:
    """One group's outcome after the check step: the action that will
    actually be proposed, and who is in or out of it, with a reason for
    every member who is out.
    """

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
    """A value from AgentLoopState, made safe and readable for a trace.

    Every group and proposal here collapses to counts and codes, never a
    client id: the same rule the tools and the model itself already follow.
    """
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


def build_plan_system_prompt(as_of: date) -> str:
    """The instructions for the plan step: look around, then write in plain words."""
    return (
        "You help run outreach for a wealth manager, one night at a time.\n"
        f"Today is {as_of.isoformat()}.\n"
        "Use the tools you are given to look at tonight's groups of clients, "
        "what has been sent to each group recently, and how much of today's "
        "sending allowance is left.\n"
        "You may only ever look at groups. Never ask for one client by name.\n"
        "Once you have looked, write a short plan in plain, everyday words.\n"
        "Say which groups matter tonight and which ones can be left alone, and why.\n"
        "Do not choose an action yet, that comes later.\n"
        "When you are ready, reply with only the plan, in a few short sentences, "
        "and no further tool call."
    )


def build_choose_system_prompt(
    *,
    actions: Mapping[str, AgentActionCatalog],
    candidate_group_names: Sequence[str],
    plan_text: str,
    as_of: date,
) -> str:
    """The instructions for the choose step: pick from a fixed menu, or leave a
    group alone, one choose_action call per group.
    """
    menu = "\n".join(
        f"- {code}: {row.title}. Who it is for: {row.who}. "
        + (f"Uses the angle '{row.message_angle}'." if row.message_angle else "Sends nothing.")
        for code, row in sorted(actions.items())
    )
    groups = ", ".join(sorted(candidate_group_names))
    return (
        "You choose tonight's outreach action for a wealth manager, one group at a time.\n"
        f"Today is {as_of.isoformat()}.\n"
        f"Tonight's plan:\n{plan_text}\n\n"
        f"These groups need a decision tonight: {groups}\n"
        "You may call a tool to look closer at a group, or check the allowance "
        "left, before you decide.\n"
        "Choose only from this list of actions, written exactly as shown:\n"
        f"{menu}\n\n"
        f"Call {CHOOSE_ACTION_TOOL_NAME} once for every group listed above: either a "
        "real action code, or 'do_nothing' to leave that group alone tonight.\n"
        "Use the exact angle written above for the action you chose, or leave angle "
        "out for an action that sends nothing.\n"
        "If a call comes back with an error, read why and call it again for that "
        "group with a corrected answer.\n"
        "Never invent an action code or an angle that is not in the list above.\n"
        "Write the reason in plain, everyday words that explain why this group "
        "matters tonight.\n"
        f"Once you have called {CHOOSE_ACTION_TOOL_NAME} for every group, reply with "
        "a short line of plain text and no further tool call."
    )


def build_choose_action_tool_spec(
    *, actions: Mapping[str, AgentActionCatalog], candidate_group_names: Sequence[str]
) -> ToolSpec:
    """The tool the choose step's model calls to record one group's decision.

    Offered only during choose, alongside the read tools: this is how the
    step's answer leaves the model, so there is no reply text left to parse.
    """
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
    choices: dict[str, AgentChoice],
    refusals: list[str],
) -> Callable[..., dict[str, Any]]:
    """Build the choose_action tool function for one attempt at the choose step.

    Recording happens by writing into `choices`, so the caller reads it back
    once the conversation ends; every refusal's message is appended to
    `refusals` too, so a run that never settles can still say what the model
    last got wrong. Nothing here raises: a bad call comes back as a plain
    error dict the model sees on its next turn, the same way every other
    tool refuses, so the model can correct itself instead of the run falling
    back straight away.
    """
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


def build_agent_loop_graph(
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
) -> CompiledStateGraph:
    """Wire the six steps into a compiled graph, ready to invoke() once."""
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
        system_prompt = build_plan_system_prompt(as_of)
        tally = ModelCallTally()
        call_tool = traced_tool_call(
            make_tool_executor(session, run_id, events=events),
            tracer=tracer,
            trace_id=trace_id,
            parent=span,
        )
        converse = traced_converse(
            counting_converse(
                as_converse_call(llm_client, system=system_prompt, tools=TOOL_SPECS), tally
            ),
            tracer=tracer,
            trace_id=trace_id,
            model=llm_client.model,
            parent=span,
            system=system_prompt,
        )

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
        if result.stopped_reason != "final_answer" or not plan_text:
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
        session.commit()
        return {
            "plan_text": plan_text,
            "model_call_count": tally.calls,
            "input_tokens": tally.input_tokens,
            "output_tokens": tally.output_tokens,
        }

    def choose(state: AgentLoopState, span: Any) -> dict[str, Any]:
        groups = state["groups"]
        actions = state["actions"]
        candidate_names = [g.name for g in groups if g.members]

        choices: dict[str, AgentChoice] = {}
        fell_back = False
        last_error: str | None = None
        attempts = 0
        tally = ModelCallTally()

        if candidate_names:
            fell_back = True
            system_prompt = build_choose_system_prompt(
                actions=actions,
                candidate_group_names=candidate_names,
                plan_text=state["plan_text"],
                as_of=as_of,
            )
            tools = (
                *TOOL_SPECS,
                build_choose_action_tool_spec(
                    actions=actions, candidate_group_names=candidate_names
                ),
            )
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

            if choice is None or choice.action_code == DO_NOTHING_ACTION:
                action = load_action_or_raise(session, DO_NOTHING_ACTION, as_of)
                reason = (
                    choice.reason
                    if choice is not None
                    else "the model did not choose an action for this group tonight"
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

            action = load_action_or_raise(session, choice.action_code, as_of)
            skip_reasons = group_skip_reasons(session, group.members, action, as_of, cooldown_days)
            included = tuple(
                member for member in group.members if skip_reasons[member_key(member)] is None
            )
            if not included:
                action = load_action_or_raise(session, DO_NOTHING_ACTION, as_of)

            checked[group.name] = GroupDecision(
                action=action,
                angle=choice.angle,
                reason=choice.reason,
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
        return {"checked": checked}

    def propose(state: AgentLoopState, _span: Any) -> dict[str, Any]:
        proposals: list[AgentProposal] = []
        for group in state["groups"]:
            decision = state["checked"].get(group.name)
            if decision is None:
                continue

            proposal = AgentProposal(
                run_id=run_id,
                action_code=decision.action.action_code,
                catalog_version=decision.action.version,
                group_name=group.name,
                group_definition=dict(group.definition),
                client_count=group.client_count,
                money_total_kes=sum(member.balance for member in decision.included),
                evidence=group_evidence(group, state["thresholds"]),
                reason=decision.reason,
                angle=decision.action.message_angle,
                content_mix=decision.action.content_mix,
                permission_applied=effective_permission(session, decision.action.action_code),
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
                },
            )
            events.record(
                PROPOSAL_CREATED,
                proposal_id=proposal.proposal_id,
                group_name=group.name,
                action_code=decision.action.action_code,
                included_count=len(decision.included),
                permission_applied=proposal.permission_applied,
            )
            if (
                decision.action.action_code != DO_NOTHING_ACTION
                and proposal.permission_applied != ACTS_ALONE
            ):
                events.record(
                    APPROVAL_NEEDED,
                    proposal_id=proposal.proposal_id,
                    group_name=group.name,
                    action_code=decision.action.action_code,
                    included_count=len(decision.included),
                )
            proposals.append(proposal)

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

    graph = StateGraph(AgentLoopState)
    graph.add_node("gather", _traced("gather", gather))
    graph.add_node("plan", _traced("plan", plan))
    graph.add_node("choose", _traced("choose", choose))
    graph.add_node("check", _traced("check", check))
    graph.add_node("propose", _traced("propose", propose))
    graph.add_node("report", _traced("report", report))

    graph.add_edge(START, "gather")
    graph.add_edge("gather", "plan")
    graph.add_edge("plan", "choose")
    graph.add_edge("choose", "check")
    graph.add_edge("check", "propose")
    graph.add_edge("propose", "report")
    graph.add_edge("report", END)

    return graph.compile()


def _latest_completed_risk_run_id(session: Session) -> str | None:
    """The most recently finished risk run, or None if there has never been
    one. The agent reads whatever is on file at the moment it runs; this is
    only a record of which risk run that happened to be, not a dependency.
    """
    return session.scalar(
        select(RiskRun.run_id)
        .where(RiskRun.state == "completed")
        .order_by(RiskRun.finished_at.desc())
        .limit(1)
    )


def start_agent_run(
    session: Session,
    *,
    trigger: str,
    as_of: date | None = None,
    kind: str = NIGHTLY_AGENT,
    insight_id: int | None = None,
) -> AgentRun:
    """Open a new agent_run row and commit it, ready for execute_agent_run.

    A run that reads the whole book refuses with AgentRunInProgress when
    another one of those is already under way, rather than queuing silently.
    An action run answers one finding a person has just accepted, so it
    starts whatever else is going and holds nothing up itself. Records which
    risk run's data is on file right now, or that there is none -- the agent
    never needs a risk run to exist.
    """
    as_of = as_of or date.today()
    if kind in BOOK_WIDE_AGENTS:
        in_progress = session.scalar(
            select(AgentRun)
            .where(AgentRun.state == "running", AgentRun.agent_kind.in_(BOOK_WIDE_AGENTS))
            .limit(1)
        )
        if in_progress is not None:
            raise AgentRunInProgress(f"agent run {in_progress.run_id} is already running")

    risk_run_id = _latest_completed_risk_run_id(session)
    run = AgentRun(trigger=trigger, risk_run_id=risk_run_id, agent_kind=kind, insight_id=insight_id)
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
    """Run the six-step graph against an agent_run row start_agent_run
    already opened.

    Always returns the agent_run row, whether it finished or failed. A step
    that raises leaves the run in the failed state with the reason recorded,
    and never leaves a half written proposal behind: everything a failing
    step had not yet committed is rolled back before the run is marked
    failed. Split out from start_agent_run so a caller that must answer
    quickly, such as the API, can hand the run id back the moment it exists
    and run this part in the background.
    """
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

    graph = build_agent_loop_graph(
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
            graph.invoke({})
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
) -> AgentRun:
    """Start a run and take it all the way through, in one call.

    The entry point for a caller that can afford to wait: a script, the
    optional hook after the risk run, and the tests. The API instead calls
    start_agent_run and execute_agent_run separately, so it can answer with
    the run id before the run has finished.
    """
    run = start_agent_run(session, trigger=trigger, as_of=as_of)
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
