from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.agents.agent_loop import MissingPromptTemplate, start_agent_run
from app.agents.events import EventLog, RunEventLog
from app.agents.insight_tools import insight_tool_specs, make_insight_tools
from app.agents.prompt_versioning import AGENT_CHAT, active_prompt
from app.agents.query_fields import FIELD_NAMES, MEASURES
from app.agents.query_tools import QUERY_TOOL_SPECS
from app.agents.run_cost import run_cost_kes
from app.agents.tool_runtime import (
    CallBudget,
    OrdinalSource,
    make_investigation_tool_executor,
)
from app.agents.tools import TOOL_SPECS
from app.agents.write_tools import WRITE_TOOL_SPECS, make_write_tools
from app.config import get_settings
from app.db.async_session import AsyncSessionLocal
from app.db.models.agent_event import (
    ERROR,
    RUN_COMPLETED,
    RUN_STARTED,
    RUN_STATUS,
    WARNING,
)
from app.db.models.agent_run import CHAT_AGENT, AgentRun
from app.db.models.chat import ChatSession, ChatTurn
from app.db.session import SessionLocal
from app.llmops.spans import ModelCallTally, traced_aconverse, traced_async_tool_call
from app.llmops.tracing import NullTracer, Tracer, get_shared_tracer
from app.privacy.boundary import AuditSink, run_conversation_boundary_async
from app.privacy.llm_client import (
    AsyncConversingLLMClient,
    ConversationTurn,
    ToolSpec,
    get_agent_llm_client,
)

logger = structlog.get_logger(__name__)

ANSWER_READY = "answer_ready"
RAN_OUT_OF_TURNS = "ran_out_of_turns"

FALLBACK_ANSWER = (
    "I could not finish answering within the step limit for this question. "
    "Please try asking it in a simpler way."
)

_MAX_TITLE_LENGTH = 120


def chat_tool_specs() -> tuple[ToolSpec, ...]:
    return (*TOOL_SPECS, *QUERY_TOOL_SPECS, *insight_tool_specs(), *WRITE_TOOL_SPECS)


def build_chat_system_prompt(session: Session, question: str, as_of: date) -> tuple[str, int]:
    row = active_prompt(session, AGENT_CHAT, as_of)
    if row is None:
        raise MissingPromptTemplate(f"no published '{AGENT_CHAT}' prompt as of {as_of}")
    prompt = row.template.format(
        as_of=as_of.isoformat(),
        question=question,
        field_names=", ".join(FIELD_NAMES),
        measures=", ".join(MEASURES),
    )
    return prompt, row.version


def start_chat_turn(
    session: Session,
    *,
    question: str,
    session_id: int | None = None,
    reviewer_id: str | None = None,
    as_of: date | None = None,
) -> ChatTurn:
    question = question.strip()
    if not question:
        raise ValueError("a chat question cannot be empty")

    if session_id is None:
        chat_session = ChatSession(reviewer_id=reviewer_id, title=question[:_MAX_TITLE_LENGTH])
        session.add(chat_session)
        session.flush()
        session_id = chat_session.session_id
    elif session.get(ChatSession, session_id) is None:
        raise ChatSessionNotFound(session_id)

    run = start_agent_run(session, trigger="chat", as_of=as_of, kind=CHAT_AGENT)
    turn = ChatTurn(session_id=session_id, run_id=run.run_id, question=question)
    session.add(turn)
    session.commit()
    return turn


class ChatSessionNotFound(Exception):
    pass


def _counting_converse(
    llm_client: AsyncConversingLLMClient,
    *,
    system: str,
    tools: tuple[ToolSpec, ...],
    tally: ModelCallTally,
):
    async def call(messages: list[dict[str, Any]]) -> ConversationTurn:
        turn = await llm_client.aconverse(system=system, messages=messages, tools=tools)
        tally.add(turn)
        return turn

    return call


async def execute_chat_turn(
    session: Session,
    turn: ChatTurn,
    *,
    llm_client: AsyncConversingLLMClient | None = None,
    as_of: date | None = None,
    cooldown_days: int | None = None,
    max_turns: int | None = None,
    tracer: Tracer | None = None,
    audit: AuditSink | None = None,
    events: EventLog | None = None,
) -> AgentRun:
    llm_client = llm_client or get_agent_llm_client()
    day = as_of or date.today()
    tracer = tracer or NullTracer()
    settings = get_settings()
    max_turns = settings.agent_chat_max_turns if max_turns is None else max_turns
    run_id = turn.run_id
    turn_id = turn.turn_id
    question = turn.question
    trace_id = uuid.uuid4().hex
    own_events = events is None
    events = RunEventLog(run_id) if own_events else events

    events.record(
        RUN_STARTED, agent="chat", trigger="chat", as_of=day.isoformat(), model=llm_client.model
    )
    logger.info("chat.run_executing", run_id=run_id, trace_id=trace_id, model=llm_client.model)

    try:
        answer, tally = await _answer(
            session,
            run_id=run_id,
            question=question,
            day=day,
            cooldown_days=cooldown_days,
            max_turns=max_turns,
            llm_client=llm_client,
            trace_id=trace_id,
            tracer=tracer,
            audit=audit,
            events=events,
        )
    except Exception as exc:
        logger.exception("chat.run_failed", run_id=run_id, reason=str(exc))
        events.record(ERROR, about="run", reason=str(exc))
        events.record(RUN_COMPLETED, state="failed", reason=str(exc))
        await asyncio.to_thread(_mark_failed, session, run_id, str(exc))
        await asyncio.to_thread(tracer.flush)
        if own_events:
            await asyncio.to_thread(events.close)
        return session.get(AgentRun, run_id)

    cost_kes = await asyncio.to_thread(run_cost_kes, session, llm_client.model, day, tally.calls)
    await asyncio.to_thread(_finish, session, run_id, turn_id, answer, cost_kes)
    await asyncio.to_thread(tracer.flush)
    events.record(RUN_STATUS, stage=ANSWER_READY, answer=answer)
    events.record(RUN_COMPLETED, state="completed", cost_kes=cost_kes)
    logger.info("chat.run_completed", run_id=run_id, trace_url=tracer.get_trace_url(trace_id))
    if own_events:
        await asyncio.to_thread(events.close)
    return session.get(AgentRun, run_id)


async def _answer(
    session: Session,
    *,
    run_id: int,
    question: str,
    day: date,
    cooldown_days: int | None,
    max_turns: int,
    llm_client: AsyncConversingLLMClient,
    trace_id: str,
    tracer: Tracer,
    audit: AuditSink | None,
    events: EventLog,
) -> tuple[str, ModelCallTally]:
    settings = get_settings()
    system_prompt, _version = await asyncio.to_thread(
        build_chat_system_prompt, session, question, day
    )
    ordinals = await asyncio.to_thread(OrdinalSource.from_database, session, run_id)
    write_tools = make_insight_tools(run_id=run_id, events=events)
    write_tools.update(
        make_write_tools(run_id=run_id, as_of=day, cooldown_days=cooldown_days, events=events)
    )
    tally = ModelCallTally()
    span = tracer.start_span(
        trace_id=trace_id,
        name="chat",
        input={"question": question, "system": system_prompt},
        metadata={"run_id": run_id, "max_turns": max_turns},
        as_type="agent",
    )
    blocking_session = SessionLocal()
    try:
        async with AsyncSessionLocal() as query_session:
            call_tool = traced_async_tool_call(
                make_investigation_tool_executor(
                    blocking_session=blocking_session,
                    query_session=query_session,
                    run_id=run_id,
                    write_tools=write_tools,
                    ordinals=ordinals,
                    query_budget=CallBudget(settings.agent_query_call_budget),
                    events=events,
                ),
                tracer=tracer,
                trace_id=trace_id,
                parent=span,
            )
            converse = traced_aconverse(
                _counting_converse(
                    llm_client, system=system_prompt, tools=chat_tool_specs(), tally=tally
                ),
                tracer=tracer,
                trace_id=trace_id,
                model=llm_client.model,
                parent=span,
                system=system_prompt,
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
        await asyncio.to_thread(blocking_session.commit)
    except Exception:
        await asyncio.to_thread(blocking_session.rollback)
        tracer.end_span(span, output={"error": "chat_failed"}, level="ERROR")
        raise
    finally:
        await asyncio.to_thread(blocking_session.close)

    answer = (result.final_text or "").strip()
    if result.stopped_reason != "final_answer" or not answer:
        answer = FALLBACK_ANSWER
        events.record(WARNING, about="run", reason=RAN_OUT_OF_TURNS)
    tracer.end_span(span, output={"answer": answer, **tally.as_detail()})
    return answer, tally


def _finish(
    session: Session, run_id: int, turn_id: int, answer: str, cost_kes: float | None
) -> None:
    turn = session.get(ChatTurn, turn_id)
    turn.answer = answer
    run = session.get(AgentRun, run_id)
    run.state = "completed"
    run.summary = answer
    run.cost_kes = cost_kes
    run.finished_at = datetime.now(UTC)
    session.commit()


def _mark_failed(session: Session, run_id: int, reason: str) -> None:
    session.rollback()
    run = session.get(AgentRun, run_id)
    run.state = "failed"
    run.failure_reason = reason
    run.finished_at = datetime.now(UTC)
    session.commit()


def run_chat_in_background(turn_id: int, *, as_of: date | None = None) -> None:
    with SessionLocal() as session:
        turn = session.get(ChatTurn, turn_id)
        if turn is None:
            return
        asyncio.run(
            execute_chat_turn(
                session,
                turn,
                llm_client=get_agent_llm_client(),
                as_of=as_of,
                tracer=get_shared_tracer(),
            )
        )
