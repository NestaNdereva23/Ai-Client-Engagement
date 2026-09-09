"""The action agent: one accepted finding in, one proposal out.

A person read what the other agent found and said yes to it. This graph
decides how the business should answer: it reads the finding, picks one
response from the live catalogue, works out which client funds it covers,
runs every check before anything is written, and writes the proposal with
the finding recorded on it.

It never sends anything. A proposal is still a proposal, and the review and
send gates in front of it are unchanged.

Two rules shape the checks. A response that reaches the client runs every
contact check: the do not contact list, an open complaint, how recently the
client was contacted, and whether the angle is held. A response that stays
inside the business, a task for an adviser, an escalation, a change of
handling, or a note to watch, contacts nobody, so those checks do not apply
to it. Either way every client left out is written down with the reason.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.orm import Session

from app.agents.action_catalog import selectable_actions
from app.agents.agent_loop import ACTS_ALONE, GroupDecision, start_agent_run
from app.agents.events import NO_EVENTS, EventLog, RunEventLog
from app.agents.insight_members import ResolvedMembers, resolve_insight_members
from app.agents.insight_state import transition_insight
from app.agents.permissions import effective_permission
from app.agents.propose import (
    DO_NOTHING_ACTION,
    group_skip_reasons,
    load_action_or_raise,
    member_key,
    skip_reason_counts,
)
from app.agents.run_cost import run_cost_kes
from app.agents.tool_runtime import make_tool_executor
from app.agents.tools import TOOL_SPECS
from app.agents.watchlist import GroupMember
from app.audit.log import record_audit
from app.db.models.agent import CONTACTING_RESPONSE_KINDS, AgentActionCatalog
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
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.agent_run import ACTION_AGENT, AgentRun
from app.llmops.spans import ModelCallTally, counting_converse, traced_converse, traced_tool_call
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

CHOOSE_RESPONSE_TOOL_NAME = "choose_response"

DEFAULT_CHOOSE_MAX_TURNS = 6
DEFAULT_MAX_CHOOSE_ATTEMPTS = 2

ACCEPTED = "accepted"
ACTED_ON = "acted_on"

NO_CLIENTS_FOUND = "no_clients_found"

NOT_CHOSEN = "no_response_was_chosen"

MODEL_CHOSE_NOTHING = "the model did not settle on a response for this finding"


class InsightNotActionable(Exception):
    """The finding is not in a state where a response may be decided."""


@dataclass(frozen=True)
class InsightBrief:
    """One accepted finding, as the action agent is handed it.

    Counts, bands and the finding's own words only. No client id and no
    client name reaches this shape, the same rule everything the agent reads
    already follows.
    """

    insight_id: int
    kind: str
    title: str
    group_name: str
    client_count: int
    money_total_kes: float | None
    confidence: str
    confidence_reason: str
    suggestion: str
    why_now: str
    avoid_saying: str | None


def brief_for(insight: AgentInsight) -> InsightBrief:
    """The finding, cut down to what the model may read."""
    return InsightBrief(
        insight_id=insight.insight_id,
        kind=insight.kind,
        title=insight.title,
        group_name=insight.group_name,
        client_count=insight.client_count,
        money_total_kes=insight.money_total_kes,
        confidence=insight.confidence,
        confidence_reason=insight.confidence_reason,
        suggestion=insight.suggestion,
        why_now=insight.why_now,
        avoid_saying=insight.avoid_saying,
    )


class ActionAgentState(TypedDict, total=False):
    """State threaded through the five steps of one action run."""

    brief: InsightBrief
    actions: dict[str, AgentActionCatalog]
    resolved: ResolvedMembers
    choice: AgentChoice | None
    decision: GroupDecision
    proposal: AgentProposal | None
    summary: str
    cost_kes: float | None
    model_call_count: int
    input_tokens: int
    output_tokens: int


def _trace_action(action: AgentActionCatalog) -> dict[str, Any]:
    return {
        "action_code": action.action_code,
        "title": action.title,
        "response_kind": action.response_kind,
    }


def _trace_decision(decision: GroupDecision) -> dict[str, Any]:
    return {
        "action_code": decision.action.action_code,
        "angle": decision.angle,
        "reason": decision.reason,
        "included_count": len(decision.included),
        "excluded_count": len(decision.skip_reasons) - len(decision.included),
    }


def _trace_value(value: Any) -> Any:
    """A value from the state, made safe and readable for a trace.

    Everything here collapses to counts and codes. The resolved members are
    the one part carrying client keys, so only their size is traced.
    """
    if isinstance(value, AgentActionCatalog):
        return _trace_action(value)
    if isinstance(value, GroupDecision):
        return _trace_decision(value)
    if isinstance(value, ResolvedMembers):
        return {
            "member_count": len(value.members),
            "source": value.source,
            "refusal": value.refusal,
        }
    if isinstance(value, InsightBrief):
        return {
            "insight_id": value.insight_id,
            "kind": value.kind,
            "group_name": value.group_name,
            "client_count": value.client_count,
        }
    if isinstance(value, AgentProposal):
        return {
            "proposal_id": value.proposal_id,
            "action_code": value.action_code,
            "insight_id": value.insight_id,
            "client_count": value.client_count,
            "status": value.status,
        }
    if isinstance(value, AgentChoice):
        return value.model_dump()
    if isinstance(value, Mapping):
        return {str(key): _trace_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_trace_value(item) for item in value]
    return value


def _span_values(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _trace_value(value) for key, value in values.items()}


def build_choose_system_prompt(
    *, brief: InsightBrief, actions: Mapping[str, AgentActionCatalog], as_of: date
) -> str:
    """The instructions for the choose step: one finding, one response."""
    menu = "\n".join(
        f"- {code}: {row.title}. Who it is for: {row.who}. "
        + (
            f"Sends an email using the angle '{row.message_angle}'."
            if row.message_angle
            else "Sends nothing to the client."
        )
        for code, row in sorted(actions.items())
    )
    money = (
        f"about {brief.money_total_kes:,.0f} KES"
        if brief.money_total_kes is not None
        else "an amount that was not recorded"
    )
    avoid = brief.avoid_saying or "nothing in particular was named."
    return (
        "You decide how a wealth manager should answer one thing that was "
        "found in the client book.\n"
        f"Today is {as_of.isoformat()}.\n"
        "A person has already read this finding and agreed it is worth acting on. "
        "Your job is only to pick the response.\n\n"
        f"What was found: {brief.title}\n"
        f"The sort of finding: {brief.kind}\n"
        f"Who it is about: {brief.group_name}, {brief.client_count} clients holding {money}\n"
        f"Why it matters now: {brief.why_now}\n"
        f"What the finding suggests: {brief.suggestion}\n"
        f"How sure the finding is: {brief.confidence}, because {brief.confidence_reason}\n"
        f"What a message about this must not claim: {avoid}\n\n"
        "You may call a tool to look closer at the group, at what was proposed "
        "for it before, or at how much of today's allowance is left, before you "
        "decide. You may only ever look at groups. Never ask for one client by name.\n\n"
        "Choose one response from this list, written exactly as shown:\n"
        f"{menu}\n\n"
        f"Record your decision by calling {CHOOSE_RESPONSE_TOOL_NAME} exactly once. "
        "Use the exact angle written above for the response you chose, or leave "
        "angle out for a response that sends nothing.\n"
        "If the call comes back with an error, read why and call it again with a "
        "corrected answer.\n"
        "Never invent a response code or an angle that is not in the list above.\n"
        "Write the reason in plain, everyday words that say why this response fits "
        "this finding.\n"
        f"Once you have called {CHOOSE_RESPONSE_TOOL_NAME}, reply with a short line "
        "of plain text and no further tool call."
    )


def build_choose_response_tool_spec(*, actions: Mapping[str, AgentActionCatalog]) -> ToolSpec:
    """The tool the choose step's model calls to record its decision."""
    return ToolSpec(
        name=CHOOSE_RESPONSE_TOOL_NAME,
        description=(
            "Record how the business should answer this finding: one response "
            "code from the menu. Call this once, then reply with plain text and "
            "no further tool call. Calling it again replaces your answer."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action_code": {
                    "type": "string",
                    "description": "One response code from the menu, written exactly as shown.",
                    "enum": sorted(actions),
                },
                "angle": {
                    "type": ["string", "null"],
                    "description": "The response's exact angle, or null when it sends nothing.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this response fits this finding, in plain everyday words.",
                },
            },
            "required": ["action_code", "reason"],
        },
    )


def make_choose_response_tool(
    *,
    group_name: str,
    actions: Mapping[str, AgentActionCatalog],
    recorded: dict[str, AgentChoice],
    refusals: list[str],
) -> Callable[..., dict[str, Any]]:
    """Build the choose_response tool for one attempt at the choose step.

    The answer leaves the model as a tool call, so there is no reply text to
    parse, and the code and the angle are checked against the live catalogue
    here rather than trusted. Nothing raises: a bad call comes back as a
    plain error the model sees on its next turn and can correct.
    """

    def choose_response(
        session: Session,
        *,
        action_code: str,
        reason: str,
        angle: str | None = None,
    ) -> dict[str, Any]:
        action = actions.get(action_code)
        if action is None:
            message = f"'{action_code}' is not on the list of responses"
            refusals.append(message)
            return {"error": "unknown_action", "message": message}

        if angle != action.message_angle:
            message = f"'{action_code}' takes the angle {action.message_angle!r}, not {angle!r}"
            refusals.append(message)
            return {"error": "wrong_angle", "message": message}

        if not reason.strip():
            message = "give a short reason for this response"
            refusals.append(message)
            return {"error": "missing_reason", "message": message}

        recorded["choice"] = AgentChoice(
            group_name=group_name, action_code=action_code, angle=angle, reason=reason
        )
        return {
            "status": "recorded",
            "action_code": action_code,
            "response_kind": action.response_kind,
            "angle": angle,
        }

    return choose_response


def contact_checks_apply(action: AgentActionCatalog) -> bool:
    """Whether this response reaches the client, so the contact checks run."""
    return action.response_kind in CONTACTING_RESPONSE_KINDS


def gate_members(
    session: Session,
    *,
    action: AgentActionCatalog,
    members: Sequence[GroupMember],
    as_of: date,
    cooldown_days: int | None,
) -> dict[tuple[int, int], str | None]:
    """Why each client fund was left out of this response, or None if it stays."""
    if contact_checks_apply(action):
        return group_skip_reasons(session, members, action, as_of, cooldown_days)
    return {member_key(member): None for member in members}


def insight_evidence(brief: InsightBrief) -> str:
    """The plain language evidence behind the proposal, from the finding."""
    return (
        f"{brief.title}, covering {brief.client_count} clients. Why it matters now: {brief.why_now}"
    )


def build_action_agent_graph(
    *,
    session: Session,
    run_id: int,
    insight_id: int,
    trace_id: str,
    as_of: date,
    llm_client: ConversingLLMClient,
    cooldown_days: int | None = None,
    max_choose_attempts: int = DEFAULT_MAX_CHOOSE_ATTEMPTS,
    tracer: Tracer | None = None,
    audit: AuditSink | None = None,
    events: EventLog = NO_EVENTS,
) -> CompiledStateGraph:
    """Wire the five steps into a compiled graph, ready to invoke() once."""
    tracer = tracer or NullTracer()

    def _traced(name: str, fn):
        """One span per step, handed to the step so it can nest its own work."""

        def wrapped(state: ActionAgentState) -> dict[str, Any]:
            handle = tracer.start_span(
                trace_id=trace_id,
                name=name,
                input=_span_values(dict(state)),
                metadata={"run_id": run_id, "insight_id": insight_id, "as_of": as_of.isoformat()},
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
            tracer.end_span(handle, output=_span_values(result))
            events.record(STEP_COMPLETED, step=name)
            return result

        return wrapped

    def read(state: ActionAgentState, _span: Any) -> dict[str, Any]:
        insight = session.get(AgentInsight, insight_id)
        if insight is None:
            raise InsightNotActionable(f"there is no finding {insight_id}")
        if insight.state != ACCEPTED:
            raise InsightNotActionable(
                f"finding {insight_id} is {insight.state}, and only an accepted "
                "finding may be acted on"
            )

        brief = brief_for(insight)
        actions = selectable_actions(session, as_of)
        resolved = resolve_insight_members(session, insight, as_of)

        logger.info(
            "action_agent.read",
            run_id=run_id,
            insight_id=insight_id,
            group_name=brief.group_name,
            member_count=len(resolved.members),
            member_source=resolved.source,
            refusal=resolved.refusal,
            selectable_actions=sorted(actions),
        )
        record_audit(
            session,
            entity_type="agent_run",
            action="read",
            entity_id=str(run_id),
            run_id=str(run_id),
            detail={
                "insight_id": insight_id,
                "group_name": brief.group_name,
                "member_count": len(resolved.members),
                "member_source": resolved.source,
                "refusal": resolved.refusal,
                "selectable_actions": sorted(actions),
            },
        )
        session.commit()
        events.record(
            RUN_STATUS,
            stage="read",
            insight_id=insight_id,
            group_name=brief.group_name,
            member_count=len(resolved.members),
        )
        if resolved.refusal is not None:
            events.record(WARNING, about="read", reason=NO_CLIENTS_FOUND, detail=resolved.refusal)
        return {"brief": brief, "actions": actions, "resolved": resolved}

    def choose(state: ActionAgentState, span: Any) -> dict[str, Any]:
        brief = state["brief"]
        actions = state["actions"]
        recorded: dict[str, AgentChoice] = {}
        last_error: str | None = None
        attempts = 0
        tally = ModelCallTally()

        system_prompt = build_choose_system_prompt(brief=brief, actions=actions, as_of=as_of)
        tools = (*TOOL_SPECS, build_choose_response_tool_spec(actions=actions))
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
            attempt: dict[str, AgentChoice] = {}
            refusals: list[str] = []
            call_tool = traced_tool_call(
                make_tool_executor(
                    session,
                    run_id,
                    extra_tools={
                        CHOOSE_RESPONSE_TOOL_NAME: make_choose_response_tool(
                            group_name=brief.group_name,
                            actions=actions,
                            recorded=attempt,
                            refusals=refusals,
                        )
                    },
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
            if result.stopped_reason == "final_answer" and attempt:
                recorded = attempt
                last_error = None
                break
            last_error = (
                refusals[-1]
                if refusals
                else "the model did not record a response before the turn cap"
            )

        choice = recorded.get("choice")
        logger.info(
            "action_agent.choose",
            run_id=run_id,
            insight_id=insight_id,
            action_code=None if choice is None else choice.action_code,
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
                "insight_id": insight_id,
                "action_code": None if choice is None else choice.action_code,
                "angle": None if choice is None else choice.angle,
                "attempts": attempts,
                "last_error": last_error,
                **tally.as_detail(),
            },
        )
        session.commit()
        events.record(
            RUN_STATUS,
            stage="chosen",
            action_code=None if choice is None else choice.action_code,
            attempts=attempts,
        )
        if choice is None:
            events.record(WARNING, about="choose", reason="fell_back", last_error=last_error)
        return {
            "choice": choice,
            "model_call_count": tally.calls,
            "input_tokens": tally.input_tokens,
            "output_tokens": tally.output_tokens,
        }

    def check(state: ActionAgentState, _span: Any) -> dict[str, Any]:
        choice = state["choice"]
        resolved = state["resolved"]

        if choice is None:
            decision = GroupDecision(
                action=load_action_or_raise(session, DO_NOTHING_ACTION, as_of),
                angle=None,
                reason=MODEL_CHOSE_NOTHING,
                included=(),
                skip_reasons={member_key(member): NOT_CHOSEN for member in resolved.members},
            )
        elif resolved.refusal is not None:
            decision = GroupDecision(
                action=load_action_or_raise(session, DO_NOTHING_ACTION, as_of),
                angle=None,
                reason=resolved.refusal,
                included=(),
                skip_reasons={},
            )
        else:
            action = load_action_or_raise(session, choice.action_code, as_of)
            skip_reasons = gate_members(
                session,
                action=action,
                members=resolved.members,
                as_of=as_of,
                cooldown_days=cooldown_days,
            )
            included = tuple(
                member for member in resolved.members if skip_reasons[member_key(member)] is None
            )
            if not included:
                action = load_action_or_raise(session, DO_NOTHING_ACTION, as_of)
            decision = GroupDecision(
                action=action,
                angle=choice.angle,
                reason=choice.reason,
                included=included,
                skip_reasons=skip_reasons,
            )

        logger.info(
            "action_agent.check",
            run_id=run_id,
            insight_id=insight_id,
            action_code=decision.action.action_code,
            included_count=len(decision.included),
            excluded_count=len(decision.skip_reasons) - len(decision.included),
        )
        record_audit(
            session,
            entity_type="agent_run",
            action="check",
            entity_id=str(run_id),
            run_id=str(run_id),
            detail={
                "insight_id": insight_id,
                "action_code": decision.action.action_code,
                "included_count": len(decision.included),
                "excluded_count": len(decision.skip_reasons) - len(decision.included),
                "skip_reason_counts": skip_reason_counts(decision.skip_reasons),
            },
        )
        session.commit()
        return {"decision": decision}

    def propose(state: ActionAgentState, _span: Any) -> dict[str, Any]:
        brief = state["brief"]
        decision = state["decision"]
        resolved = state["resolved"]
        insight = session.get(AgentInsight, insight_id)

        proposal = AgentProposal(
            run_id=run_id,
            insight_id=insight_id,
            action_code=decision.action.action_code,
            catalog_version=decision.action.version,
            group_name=brief.group_name,
            group_definition=(
                None if insight.group_definition is None else dict(insight.group_definition)
            ),
            client_count=len({member.client_id for member in resolved.members}),
            money_total_kes=sum(member.balance for member in decision.included),
            evidence=insight_evidence(brief),
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
            for member in resolved.members
            if member_key(member) in decision.skip_reasons
        )
        session.flush()

        transition_insight(
            session,
            insight,
            to_state=ACTED_ON,
            reason=f"proposal {proposal.proposal_id} was written for this finding",
        )

        logger.info(
            "action_agent.propose",
            run_id=run_id,
            insight_id=insight_id,
            proposal_id=proposal.proposal_id,
            action_code=decision.action.action_code,
            included_count=len(decision.included),
        )
        record_audit(
            session,
            entity_type="agent_proposal",
            action="create",
            entity_id=str(proposal.proposal_id),
            run_id=str(run_id),
            detail={
                "insight_id": insight_id,
                "group_name": brief.group_name,
                "action_code": decision.action.action_code,
                "included_count": len(decision.included),
            },
        )
        events.record(
            PROPOSAL_CREATED,
            proposal_id=proposal.proposal_id,
            insight_id=insight_id,
            group_name=brief.group_name,
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
                group_name=brief.group_name,
                action_code=decision.action.action_code,
                included_count=len(decision.included),
            )
        record_audit(
            session,
            entity_type="agent_run",
            action="propose",
            entity_id=str(run_id),
            run_id=str(run_id),
            detail={"insight_id": insight_id, "proposal_id": proposal.proposal_id},
        )
        session.commit()
        return {"proposal": proposal}

    def report(state: ActionAgentState, _span: Any) -> dict[str, Any]:
        decision = state["decision"]
        proposal = state["proposal"]
        included = len(decision.included)
        excluded = len(decision.skip_reasons) - included

        lines = []
        if decision.action.action_code == DO_NOTHING_ACTION:
            lines.append(f"Nothing is being done about this finding: {decision.reason}")
        else:
            client_word = "client" if included == 1 else "clients"
            lines.append(
                f"{decision.action.title} was proposed for {included} {client_word}, "
                "and it needs a person to approve it."
            )
        if excluded:
            lines.append(f"{excluded} clients were left out by a check.")

        cost_kes = run_cost_kes(session, llm_client.model, as_of, state.get("model_call_count", 0))
        if cost_kes is not None:
            lines.append(f"This run cost about {cost_kes:.2f} KES in model calls.")

        summary = " ".join(lines)
        logger.info("action_agent.report", run_id=run_id, summary=summary, cost_kes=cost_kes)
        record_audit(
            session,
            entity_type="agent_run",
            action="report",
            entity_id=str(run_id),
            run_id=str(run_id),
            detail={
                "insight_id": insight_id,
                "proposal_id": None if proposal is None else proposal.proposal_id,
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

    graph = StateGraph(ActionAgentState)
    graph.add_node("read", _traced("read", read))
    graph.add_node("choose", _traced("choose", choose))
    graph.add_node("check", _traced("check", check))
    graph.add_node("propose", _traced("propose", propose))
    graph.add_node("report", _traced("report", report))

    graph.add_edge(START, "read")
    graph.add_edge("read", "choose")
    graph.add_edge("choose", "check")
    graph.add_edge("check", "propose")
    graph.add_edge("propose", "report")
    graph.add_edge("report", END)

    return graph.compile()


def start_action_run(session: Session, insight_id: int, *, trigger: str = "manual") -> AgentRun:
    """Open a run for one accepted finding, ready for execute_action_run.

    Refuses a finding that is not accepted, so the only way in is a person
    saying yes to it. An action run holds nothing else up and waits for
    nothing else, because a person is sitting in front of the screen.
    """
    insight = session.get(AgentInsight, insight_id)
    if insight is None:
        raise InsightNotActionable(f"there is no finding {insight_id}")
    if insight.state != ACCEPTED:
        raise InsightNotActionable(
            f"finding {insight_id} is {insight.state}, and only an accepted finding may be acted on"
        )
    return start_agent_run(session, trigger=trigger, kind=ACTION_AGENT, insight_id=insight_id)


def execute_action_run(
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
    """Run the five step graph against a run start_action_run opened.

    Always returns the run row, whether it finished or failed. A step that
    raises leaves the run failed with the reason recorded, and whatever that
    step had not committed is rolled back first, so a run never leaves a half
    written proposal behind.
    """
    llm_client = llm_client or get_agent_llm_client()
    as_of = as_of or date.today()
    tracer = tracer or NullTracer()
    run_id = run.run_id
    insight_id = run.insight_id
    trace_id = uuid.uuid4().hex
    own_events = events is None
    events = RunEventLog(run_id) if own_events else events

    logger.info(
        "action_agent.run_executing",
        run_id=run_id,
        insight_id=insight_id,
        trace_id=trace_id,
        as_of=as_of.isoformat(),
        model=llm_client.model,
    )
    events.record(
        RUN_STARTED,
        agent="action",
        trigger=run.trigger,
        insight_id=insight_id,
        as_of=as_of.isoformat(),
        model=llm_client.model,
    )

    graph = build_action_agent_graph(
        session=session,
        run_id=run_id,
        insight_id=insight_id,
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
            logger.exception("action_agent.run_failed", run_id=run_id, reason=str(exc))
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
                detail={"insight_id": insight_id, "reason": str(exc)},
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
        logger.info("action_agent.run_completed", run_id=run_id, summary=run.summary)
        return run
    finally:
        if own_events:
            events.close()


def run_action_agent(
    session: Session,
    insight_id: int,
    *,
    trigger: str = "manual",
    llm_client: ConversingLLMClient | None = None,
    as_of: date | None = None,
    cooldown_days: int | None = None,
    max_choose_attempts: int = DEFAULT_MAX_CHOOSE_ATTEMPTS,
    tracer: Tracer | None = None,
    audit: AuditSink | None = None,
    events: EventLog | None = None,
) -> AgentRun:
    """Start a run for one accepted finding and take it all the way through."""
    run = start_action_run(session, insight_id, trigger=trigger)
    return execute_action_run(
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
