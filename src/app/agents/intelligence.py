"""The intelligence agent: one graph whose whole job is to find something
worth acting on.

It gathers what is on file, asks an open question of every group, writes
down what it found, and ends with a plain summary of the run. It never
proposes a message, starts a campaign or reaches the mailer, which is why
it is allowed to be curious.

Every group is investigated on its own, with its own turn budget, its own
sessions and its own question, and they are investigated side by side
because each one spends its time waiting on the database and on the model.
A group that fails is recorded as failed and the rest of the run carries
on without it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, date, datetime
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.orm import Session

from app.agents.agent_loop import start_agent_run
from app.agents.events import NO_EVENTS, EventLog, RunEventLog
from app.agents.group_questions import EVERYTHING_ELSE, question_for
from app.agents.insight_tools import (
    WRITE_INSIGHT,
    InsightWriteBudget,
    insight_tool_specs,
    make_insight_tools,
)
from app.agents.query_fields import FIELD_NAMES, MEASURES
from app.agents.query_tools import QUERY_TOOL_SPECS
from app.agents.run_cost import run_cost_kes
from app.agents.tool_runtime import (
    CallBudget,
    OrdinalSource,
    make_investigation_tool_executor,
)
from app.agents.tools import TOOL_SPECS, get_contact_history
from app.agents.watchlist import WatchGroup, build_watchlist, load_thresholds
from app.audit.log import record_audit
from app.config import get_settings
from app.db.async_session import AsyncSessionLocal
from app.db.models.agent_event import (
    ERROR,
    RUN_COMPLETED,
    RUN_STARTED,
    RUN_STATUS,
    STEP_COMPLETED,
    STEP_STARTED,
    WARNING,
)
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_run import INTELLIGENCE_AGENT, AgentRun
from app.db.session import SessionLocal
from app.llmops.spans import ModelCallTally, traced_aconverse, traced_async_tool_call
from app.llmops.tracing import NullTracer, Tracer
from app.privacy.boundary import AuditSink, run_conversation_boundary_async
from app.privacy.llm_client import (
    AsyncConversingLLMClient,
    ConversationTurn,
    ToolSpec,
    get_agent_llm_client,
)
from app.services.agent_insights import count_insights, list_insights

logger = structlog.get_logger(__name__)

WROTE_SOMETHING = "wrote_something"
FOUND_NOTHING = "found_nothing"
SAID_NOTHING = "said_nothing"
RAN_OUT_OF_TURNS = "ran_out_of_turns"
FAILED = "failed"

RECENT_FINDINGS_SHOWN = 8

STATES_WAITING_ON_A_PERSON = ("new", "accepted")


@dataclass(frozen=True)
class GroupBrief:
    """One group as the agent is handed it: its size and its open question."""

    name: str
    question: str
    client_count: int | None = None
    fund_count: int | None = None
    money_total_kes: float | None = None
    definition: dict[str, Any] = field(default_factory=dict)
    last_contact: dict[str, Any] = field(default_factory=dict)

    @property
    def has_members(self) -> bool:
        return self.client_count is not None


@dataclass(frozen=True)
class PastFinding:
    """One finding an earlier run wrote, and where it got to."""

    kind: str
    title: str
    group_name: str
    state: str
    written_on: str


@dataclass(frozen=True)
class GatheredContext:
    """Everything the run reads before it asks the model anything."""

    as_of: date
    briefs: tuple[GroupBrief, ...]
    past_findings: tuple[PastFinding, ...]
    waiting_on_a_person: int


@dataclass(frozen=True)
class GroupOutcome:
    """What one group's investigation came to."""

    group_name: str
    outcome: str
    insight_ids: tuple[int, ...] = ()
    insight_titles: tuple[str, ...] = ()
    dismissed_reasons: tuple[str, ...] = ()
    failure_reason: str | None = None
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class IntelligenceState(TypedDict, total=False):
    """State threaded through the four steps of one intelligence run."""

    context: GatheredContext
    outcomes: list[GroupOutcome]
    summary: str
    cost_kes: float | None
    model_call_count: int
    input_tokens: int
    output_tokens: int


def _trace_value(value: Any) -> Any:
    """A value from the state, made safe and readable for a trace.

    Nothing here carries a client id: a brief holds counts, and an outcome
    holds titles the agent wrote itself, the same rule the tools follow.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _trace_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _trace_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_trace_value(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def _span_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: _trace_value(value) for key, value in values.items()}


def _outcome_detail(outcome: GroupOutcome) -> dict[str, Any]:
    """One group's result, in the shape both the trace and the audit row use."""
    return {
        "group_name": outcome.group_name,
        "outcome": outcome.outcome,
        "insight_ids": list(outcome.insight_ids),
        "insight_titles": list(outcome.insight_titles),
        "dismissed_reasons": list(outcome.dismissed_reasons),
        "failure_reason": outcome.failure_reason,
        "model_calls": outcome.model_calls,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
    }


def _brief_for(group: WatchGroup, contact: dict[str, Any]) -> GroupBrief:
    return GroupBrief(
        name=group.name,
        question=question_for(group.name),
        client_count=group.client_count,
        fund_count=group.fund_count,
        money_total_kes=group.money_total,
        definition=dict(group.definition),
        last_contact=contact,
    )


def gather_context(session: Session, as_of: date) -> GatheredContext:
    """Read what is on file before anything is asked of the model.

    Tonight's groups and their sizes, what was last proposed for each one,
    what earlier runs found, and how much of that is still sitting with a
    person. The last group has no filter of its own, so it carries the
    question alone.
    """
    thresholds = load_thresholds(session, as_of)
    groups = build_watchlist(session, as_of, thresholds)
    briefs = [
        _brief_for(group, get_contact_history(session, group_name=group.name)) for group in groups
    ]
    briefs.append(GroupBrief(name=EVERYTHING_ELSE, question=question_for(EVERYTHING_ELSE)))

    rows, _ = list_insights(session, limit=RECENT_FINDINGS_SHOWN)
    past_findings = tuple(
        PastFinding(
            kind=row.kind,
            title=row.title,
            group_name=row.group_name,
            state=row.state,
            written_on=row.created_at.date().isoformat(),
        )
        for row in rows
    )
    waiting = sum(count_insights(session, state=state) for state in STATES_WAITING_ON_A_PERSON)
    return GatheredContext(
        as_of=as_of,
        briefs=tuple(briefs),
        past_findings=past_findings,
        waiting_on_a_person=waiting,
    )


def _group_sizes_line(context: GatheredContext) -> str:
    sized = [brief for brief in context.briefs if brief.has_members]
    return "\n".join(
        f"- {brief.name}: {brief.client_count} clients across {brief.fund_count} client funds"
        for brief in sized
    )


def _past_findings_lines(context: GatheredContext) -> str:
    if not context.past_findings:
        return "Nothing has been written down before this run."
    return "\n".join(
        f"- {finding.written_on}, {finding.group_name}: {finding.title} ({finding.state})"
        for finding in context.past_findings
    )


def _last_contact_line(brief: GroupBrief) -> str:
    contact = brief.last_contact
    if not contact or not contact.get("ever_proposed"):
        return "Nothing has ever been proposed for this group."
    return (
        f"The last thing proposed for this group was {contact['action_code']} on "
        f"{contact['proposed_at'][:10]}, covering {contact['included_count']} clients, "
        f"and it is {contact['status']}."
    )


def _size_lines(brief: GroupBrief, context: GatheredContext) -> str:
    if not brief.has_members:
        return (
            "This group has no filter of its own. It is the whole book minus "
            "nothing, and it is where you may look for something none of the "
            "other groups would catch. Tonight's other groups are:\n"
            f"{_group_sizes_line(context)}"
        )
    return (
        f"It holds {brief.client_count} clients across {brief.fund_count} client funds, "
        f"and about {brief.money_total_kes:,.0f} KES.\n"
        f"The filter behind it: {brief.definition}"
    )


def build_investigation_system_prompt(*, brief: GroupBrief, context: GatheredContext) -> str:
    """The instructions for one group's investigation.

    The question comes from one place and is handed over whole. Nothing
    here offers a list of actions to pick from: the agent is asked to find
    out whether there is anything worth a person's attention, and to say so
    plainly either way.
    """
    return (
        "You look for things worth acting on in a wealth manager's client book.\n"
        f"Today is {context.as_of.isoformat()}.\n"
        "You never send anything and you never propose a message. You write down "
        "what you found, and a person decides what happens next.\n\n"
        f"Tonight you are looking at one group: {brief.name}.\n"
        f"The question to answer about it: {brief.question}\n\n"
        f"{_size_lines(brief, context)}\n"
        f"{_last_contact_line(brief)}\n\n"
        f"What earlier runs already wrote down:\n{_past_findings_lines(context)}\n"
        f"{context.waiting_on_a_person} of those findings are still waiting on a person, "
        "so do not write the same thing again.\n\n"
        "You may ask your own questions of the data with measure_slice, "
        "compare_slices, distribution and trend. You send a filter, never SQL: a "
        "list of conditions, each a field, an operator and a value.\n"
        f"Fields you may filter on: {', '.join(FIELD_NAMES)}.\n"
        f"Measures you may ask for: {', '.join(MEASURES)}.\n"
        "Answers come back as counts and rounded money. You will never see a row, "
        "a name, an exact balance or an exact date, and you must never ask for one.\n\n"
        "When you have found something worth a person's attention, call "
        "write_insight, then call add_fact for every number you want to stand "
        "behind, each with the filter it came from. A finding may cover more than "
        "this one group, and one group may be worth several findings or none.\n"
        "If this group holds nothing worth raising, call dismiss_group and say why. "
        "That is a real answer, not a failure.\n"
        "When you are done, reply with one short line of plain text and no further "
        "tool call."
    )


def investigation_tool_specs() -> tuple[ToolSpec, ...]:
    """Everything one investigation may call: read, ask, and write down."""
    return (*TOOL_SPECS, *QUERY_TOOL_SPECS, *insight_tool_specs())


def _counting_converse(
    llm_client: AsyncConversingLLMClient,
    *,
    system: str,
    tools: Sequence[ToolSpec],
    tally: ModelCallTally,
):
    """The model call, wrapped so the run can say what it spent."""

    async def call(messages: list[dict[str, Any]]) -> ConversationTurn:
        turn = await llm_client.aconverse(system=system, messages=messages, tools=tools)
        tally.add(turn)
        return turn

    return call


async def investigate_group(
    *,
    brief: GroupBrief,
    context: GatheredContext,
    run_id: int,
    trace_id: str,
    llm_client: AsyncConversingLLMClient,
    budget: InsightWriteBudget,
    ordinals: OrdinalSource,
    max_turns: int,
    query_budget: int,
    tracer: Tracer,
    parent_span: Any = None,
    audit: AuditSink | None = None,
    events: EventLog = NO_EVENTS,
) -> GroupOutcome:
    """Investigate one group and return what it came to.

    The group gets its own sessions, so nothing it writes is visible to
    another group until it commits, and its own turn and query budgets, so
    it cannot spend the whole run. A conversation that raises rolls the group's work
    back untouched rather than leaving a finding half written.
    """
    dismissals: list[dict[str, Any]] = []
    written_ids: list[int] = []
    write_tools = make_insight_tools(
        run_id=run_id, dismissals=dismissals, budget=budget, events=events
    )
    inner_write = write_tools[WRITE_INSIGHT]

    def write_insight(session: Session, **arguments: Any) -> dict[str, Any]:
        result = inner_write(session, **arguments)
        if result.get("status") == "written":
            written_ids.append(result["insight_id"])
        return result

    write_tools[WRITE_INSIGHT] = write_insight

    blocking_session = SessionLocal()
    tally = ModelCallTally()
    system_prompt = build_investigation_system_prompt(brief=brief, context=context)
    group_span = tracer.start_span(
        trace_id=trace_id,
        name=brief.name,
        input={"question": brief.question, "system": system_prompt},
        metadata={"group_name": brief.name, "max_turns": max_turns},
        as_type="agent",
        parent=parent_span,
    )
    events.record(RUN_STATUS, stage="group_started", group_name=brief.name)
    try:
        async with AsyncSessionLocal() as query_session:
            call_tool = traced_async_tool_call(
                make_investigation_tool_executor(
                    blocking_session=blocking_session,
                    query_session=query_session,
                    run_id=run_id,
                    write_tools=write_tools,
                    ordinals=ordinals,
                    query_budget=CallBudget(query_budget),
                    events=events,
                ),
                tracer=tracer,
                trace_id=trace_id,
                parent=group_span,
            )
            converse = traced_aconverse(
                _counting_converse(
                    llm_client,
                    system=system_prompt,
                    tools=investigation_tool_specs(),
                    tally=tally,
                ),
                tracer=tracer,
                trace_id=trace_id,
                model=llm_client.model,
                parent=group_span,
                system=system_prompt,
                metadata={"group_name": brief.name},
            )
            result = await run_conversation_boundary_async(
                {},
                converse,
                call_tool,
                max_turns=max_turns,
                run_id=str(run_id),
                trace_id=trace_id,
                audit=audit,
            )
        titles = await asyncio.to_thread(_titles_for, blocking_session, written_ids)
        await asyncio.to_thread(blocking_session.commit)
    except Exception as exc:
        logger.exception(
            "intelligence.group_failed", run_id=run_id, group_name=brief.name, reason=str(exc)
        )
        await asyncio.to_thread(blocking_session.rollback)
        failed = GroupOutcome(
            group_name=brief.name,
            outcome=FAILED,
            failure_reason=str(exc),
            model_calls=tally.calls,
            input_tokens=tally.input_tokens,
            output_tokens=tally.output_tokens,
        )
        tracer.end_span(
            group_span, output=_outcome_detail(failed), level="ERROR", status_message=str(exc)
        )
        events.record(ERROR, about="group", group_name=brief.name, reason=str(exc))
        return failed
    finally:
        await asyncio.to_thread(blocking_session.close)

    reasons = tuple(entry["reason"] for entry in dismissals)
    if written_ids:
        outcome = WROTE_SOMETHING
    elif reasons:
        outcome = FOUND_NOTHING
    elif result.stopped_reason != "final_answer":
        outcome = RAN_OUT_OF_TURNS
    else:
        outcome = SAID_NOTHING

    done = GroupOutcome(
        group_name=brief.name,
        outcome=outcome,
        insight_ids=tuple(written_ids),
        insight_titles=titles,
        dismissed_reasons=reasons,
        model_calls=tally.calls,
        input_tokens=tally.input_tokens,
        output_tokens=tally.output_tokens,
    )
    logger.info(
        "intelligence.group_done",
        run_id=run_id,
        group_name=brief.name,
        outcome=outcome,
        insight_count=len(written_ids),
        **tally.as_detail(),
    )
    tracer.end_span(group_span, output=_outcome_detail(done))
    events.record(
        RUN_STATUS,
        stage="group_done",
        group_name=brief.name,
        outcome=outcome,
        insight_count=len(written_ids),
    )
    if outcome == RAN_OUT_OF_TURNS:
        events.record(WARNING, about="group", group_name=brief.name, reason=RAN_OUT_OF_TURNS)
    return done


def _titles_for(session: Session, insight_ids: Sequence[int]) -> tuple[str, ...]:
    """The titles of the findings one group wrote, read before it commits."""
    return tuple(session.get(AgentInsight, insight_id).title for insight_id in insight_ids)


def _summarise(outcomes: Sequence[GroupOutcome], cost_kes: float | None) -> str:
    """The run's plain summary: what it found and what it cost.

    No model call is needed for this. Everything in it is counted from what
    the run already recorded.
    """
    findings = sum(len(outcome.insight_ids) for outcome in outcomes)
    groups_with_findings = sum(1 for outcome in outcomes if outcome.insight_ids)
    nothing = sum(1 for outcome in outcomes if outcome.outcome == FOUND_NOTHING)
    out_of_turns = sum(1 for outcome in outcomes if outcome.outcome == RAN_OUT_OF_TURNS)
    failed = [outcome for outcome in outcomes if outcome.outcome == FAILED]

    lines = []
    if findings:
        finding_word = "finding" if findings == 1 else "findings"
        group_word = "group" if groups_with_findings == 1 else "groups"
        lines.append(
            f"{findings} {finding_word} written, across {groups_with_findings} {group_word}."
        )
    else:
        lines.append("Nothing worth raising was found this run.")
    lines.append(f"{len(outcomes)} groups were looked at.")
    if nothing:
        lines.append(f"{nothing} of them were looked at and held nothing worth raising.")
    if out_of_turns:
        lines.append(f"{out_of_turns} ran out of turns before finishing.")
    if failed:
        names = ", ".join(outcome.group_name for outcome in failed)
        lines.append(f"{len(failed)} could not be looked at at all: {names}.")
    if cost_kes is not None:
        lines.append(f"This run cost about {cost_kes:.2f} KES in model calls.")
    return " ".join(lines)


def build_intelligence_graph(
    *,
    session: Session,
    run_id: int,
    trace_id: str,
    as_of: date,
    llm_client: AsyncConversingLLMClient,
    max_turns: int | None = None,
    concurrency: int | None = None,
    query_budget: int | None = None,
    tracer: Tracer | None = None,
    audit: AuditSink | None = None,
    events: EventLog = NO_EVENTS,
) -> CompiledStateGraph:
    """Wire the four steps into a compiled graph, ready to ainvoke() once."""
    tracer = tracer or NullTracer()
    settings = get_settings()
    max_turns = settings.agent_investigation_max_turns if max_turns is None else max_turns
    concurrency = settings.agent_investigation_concurrency if concurrency is None else concurrency
    query_budget = settings.agent_query_call_budget if query_budget is None else query_budget
    budget = InsightWriteBudget(settings.agent_insight_write_cap)

    def _traced(name: str, fn):
        """One span per step, handed to the step so it can nest its own work."""

        async def wrapped(state: IntelligenceState) -> dict[str, Any]:
            handle = tracer.start_span(
                trace_id=trace_id,
                name=name,
                input=_span_values(dict(state)),
                metadata={"run_id": run_id, "as_of": as_of.isoformat()},
            )
            events.record(STEP_STARTED, step=name)
            try:
                result = await fn(state, handle)
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

    def _gather(state: IntelligenceState) -> dict[str, Any]:
        context = gather_context(session, as_of)
        logger.info(
            "intelligence.gather",
            run_id=run_id,
            as_of=as_of.isoformat(),
            group_names=[brief.name for brief in context.briefs],
            waiting_on_a_person=context.waiting_on_a_person,
        )
        record_audit(
            session,
            entity_type="agent_run",
            action="gather",
            entity_id=str(run_id),
            run_id=str(run_id),
            detail={
                "as_of": as_of.isoformat(),
                "group_names": [brief.name for brief in context.briefs],
                "past_findings_shown": len(context.past_findings),
                "waiting_on_a_person": context.waiting_on_a_person,
            },
        )
        session.commit()
        return {"context": context}

    async def gather(state: IntelligenceState, _span: Any) -> dict[str, Any]:
        return await asyncio.to_thread(_gather, state)

    async def investigate(state: IntelligenceState, span: Any) -> dict[str, Any]:
        context = state["context"]
        ordinals = await asyncio.to_thread(OrdinalSource.from_database, session, run_id)
        limit = asyncio.Semaphore(max(1, concurrency))
        events.record(
            RUN_STATUS,
            stage="investigating",
            group_count=len(context.briefs),
            at_once=max(1, concurrency),
        )

        async def one(brief: GroupBrief) -> GroupOutcome:
            async with limit:
                return await investigate_group(
                    brief=brief,
                    context=context,
                    run_id=run_id,
                    trace_id=trace_id,
                    llm_client=llm_client,
                    budget=budget,
                    ordinals=ordinals,
                    max_turns=max_turns,
                    query_budget=query_budget,
                    tracer=tracer,
                    parent_span=span,
                    audit=audit,
                    events=events,
                )

        outcomes = list(await asyncio.gather(*(one(brief) for brief in context.briefs)))
        return {
            "outcomes": outcomes,
            "model_call_count": sum(outcome.model_calls for outcome in outcomes),
            "input_tokens": sum(outcome.input_tokens for outcome in outcomes),
            "output_tokens": sum(outcome.output_tokens for outcome in outcomes),
        }

    def _record(state: IntelligenceState) -> dict[str, Any]:
        for outcome in state["outcomes"]:
            record_audit(
                session,
                entity_type="agent_run",
                action="investigate_group",
                entity_id=str(run_id),
                run_id=str(run_id),
                detail=_outcome_detail(outcome),
            )
        session.commit()
        logger.info(
            "intelligence.record",
            run_id=run_id,
            outcomes={outcome.group_name: outcome.outcome for outcome in state["outcomes"]},
        )
        return {}

    async def record(state: IntelligenceState, _span: Any) -> dict[str, Any]:
        return await asyncio.to_thread(_record, state)

    def _summarise_step(state: IntelligenceState) -> dict[str, Any]:
        cost = run_cost_kes(session, llm_client.model, as_of, state.get("model_call_count", 0))
        summary = _summarise(state["outcomes"], cost)
        record_audit(
            session,
            entity_type="agent_run",
            action="report",
            entity_id=str(run_id),
            run_id=str(run_id),
            detail={
                "summary": summary,
                "cost_kes": cost,
                "model_calls": state.get("model_call_count", 0),
                "input_tokens": state.get("input_tokens", 0),
                "output_tokens": state.get("output_tokens", 0),
                "trace_id": trace_id,
                "trace_url": tracer.get_trace_url(trace_id),
            },
        )
        run_row = session.get(AgentRun, run_id)
        run_row.summary = summary
        run_row.cost_kes = cost
        session.commit()
        logger.info(
            "intelligence.report",
            run_id=run_id,
            summary=summary,
            cost_kes=cost,
            model_calls=state.get("model_call_count", 0),
            input_tokens=state.get("input_tokens", 0),
            output_tokens=state.get("output_tokens", 0),
        )
        return {"summary": summary, "cost_kes": cost}

    async def summarise(state: IntelligenceState, _span: Any) -> dict[str, Any]:
        return await asyncio.to_thread(_summarise_step, state)

    graph = StateGraph(IntelligenceState)
    graph.add_node("gather", _traced("gather", gather))
    graph.add_node("investigate", _traced("investigate", investigate))
    graph.add_node("record", _traced("record", record))
    graph.add_node("summarise", _traced("summarise", summarise))

    graph.add_edge(START, "gather")
    graph.add_edge("gather", "investigate")
    graph.add_edge("investigate", "record")
    graph.add_edge("record", "summarise")
    graph.add_edge("summarise", END)

    return graph.compile()


async def execute_intelligence_run(
    session: Session,
    run: AgentRun,
    *,
    llm_client: AsyncConversingLLMClient | None = None,
    as_of: date | None = None,
    max_turns: int | None = None,
    concurrency: int | None = None,
    query_budget: int | None = None,
    tracer: Tracer | None = None,
    audit: AuditSink | None = None,
    events: EventLog | None = None,
) -> AgentRun:
    """Run the four step graph against a run row start_agent_run opened.

    Always returns the run row, whether it finished or failed. A step that
    raises leaves the run failed with the reason recorded, and whatever
    that step had not committed is rolled back first. One group failing
    does not reach here: it is recorded as a failed group and the run
    carries on.
    """
    llm_client = llm_client or get_agent_llm_client()
    as_of = as_of or date.today()
    tracer = tracer or NullTracer()
    run_id = run.run_id
    trace_id = uuid.uuid4().hex
    own_events = events is None
    events = RunEventLog(run_id) if own_events else events

    logger.info(
        "intelligence.run_executing",
        run_id=run_id,
        trace_id=trace_id,
        as_of=as_of.isoformat(),
        model=llm_client.model,
    )
    events.record(
        RUN_STARTED,
        agent="intelligence",
        trigger=run.trigger,
        as_of=as_of.isoformat(),
        model=llm_client.model,
    )

    graph = build_intelligence_graph(
        session=session,
        run_id=run_id,
        trace_id=trace_id,
        as_of=as_of,
        llm_client=llm_client,
        max_turns=max_turns,
        concurrency=concurrency,
        query_budget=query_budget,
        tracer=tracer,
        audit=audit,
        events=events,
    )

    try:
        try:
            await graph.ainvoke({})
        except Exception as exc:
            logger.exception("intelligence.run_failed", run_id=run_id, reason=str(exc))
            events.record(ERROR, about="run", reason=str(exc))
            events.record(RUN_COMPLETED, state="failed", reason=str(exc))
            await asyncio.to_thread(_mark_failed, session, run_id, str(exc))
            await asyncio.to_thread(tracer.flush)
            return session.get(AgentRun, run_id)

        def finish() -> None:
            row = session.get(AgentRun, run_id)
            row.state = "completed"
            row.finished_at = datetime.now(UTC)
            session.commit()

        await asyncio.to_thread(finish)
        await asyncio.to_thread(tracer.flush)
        run = session.get(AgentRun, run_id)
        events.record(
            RUN_COMPLETED,
            state="completed",
            insight_count=count_insights(session, run_id=run_id),
            cost_kes=run.cost_kes,
        )
        logger.info(
            "intelligence.run_completed",
            run_id=run_id,
            summary=run.summary,
            trace_url=tracer.get_trace_url(trace_id),
        )
        return run
    finally:
        if own_events:
            await asyncio.to_thread(events.close)


def _mark_failed(session: Session, run_id: int, reason: str) -> None:
    session.rollback()
    run = session.get(AgentRun, run_id)
    run.state = "failed"
    run.failure_reason = reason
    run.finished_at = datetime.now(UTC)
    record_audit(
        session,
        entity_type="agent_run",
        action="failed",
        entity_id=str(run_id),
        run_id=str(run_id),
        detail={"reason": reason},
    )
    session.commit()


async def run_intelligence_agent(
    session: Session,
    *,
    trigger: str,
    llm_client: AsyncConversingLLMClient | None = None,
    as_of: date | None = None,
    max_turns: int | None = None,
    concurrency: int | None = None,
    query_budget: int | None = None,
    tracer: Tracer | None = None,
    audit: AuditSink | None = None,
    events: EventLog | None = None,
) -> AgentRun:
    """Start a run and take it all the way through, in one call."""
    run = await asyncio.to_thread(
        lambda: start_agent_run(session, trigger=trigger, as_of=as_of, kind=INTELLIGENCE_AGENT)
    )
    return await execute_intelligence_run(
        session,
        run,
        llm_client=llm_client,
        as_of=as_of,
        max_turns=max_turns,
        concurrency=concurrency,
        query_budget=query_budget,
        tracer=tracer,
        audit=audit,
        events=events,
    )
