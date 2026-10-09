from __future__ import annotations

import json
import re
from datetime import date

import pytest
from sqlalchemy import delete, select

from app.agents.chat import ANSWER_READY, execute_chat_turn, start_chat_turn
from app.db.async_session import dispose_async_engine
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_event import (
    RUN_COMPLETED,
    RUN_STARTED,
    RUN_STATUS,
    TOOL_COMPLETED,
    TOOL_STARTED,
    AgentEvent,
)
from app.db.models.agent_insight import AgentInsight, AgentInsightFact
from app.db.models.agent_proposal import AgentProposal
from app.db.models.agent_run import AgentRun, AgentToolCall
from app.db.models.audit import AuditLog
from app.db.models.chat import ChatSession, ChatTurn
from app.db.models.risk import ClientRiskFeatures
from app.db.session import SessionLocal
from app.privacy.llm_client import ConversationTurn, LLMUsage, ToolUseRequest
from app.services.agent_events import list_run_events

AS_OF = date(2026, 10, 4)

FUND_ID = 9741
CLIENT_IDS = tuple(range(974101, 974105))

_USAGE = LLMUsage(input_tokens=8, output_tokens=8)


class ScriptedClient:
    model = "fake-chat-model"

    def __init__(self, turns: list) -> None:
        self._turns = list(turns)

    async def aconverse(self, *, system, messages, tools=()):
        if not self._turns:
            raise AssertionError("the fake model ran out of turns")
        entry = self._turns.pop(0)
        return entry(messages) if callable(entry) else entry


def _final_answer(text: str) -> ConversationTurn:
    return ConversationTurn(text=text, tool_requests=(), usage=_USAGE, stop_reason="end_turn")


def _calls(*requests: tuple[str, dict]) -> ConversationTurn:
    return ConversationTurn(
        text="",
        tool_requests=tuple(
            ToolUseRequest(call_id=f"call_{index}", tool_name=name, tool_input=arguments)
            for index, (name, arguments) in enumerate(requests)
        ),
        usage=_USAGE,
        stop_reason="tool_use",
    )


def _write_insight() -> tuple[str, dict]:
    return (
        "write_insight",
        {
            "kind": "risk",
            "title": "A few accounts the fee is emptying",
            "group_name": "very small and quiet",
            "client_count": 3,
            "suggestion": "Ask a person to look before the balance runs out.",
            "why_now": "The fee will empty these soon.",
            "confidence": "medium",
            "confidence_reason": "Small counts, all pointing the same way.",
        },
    )


def _last_insight_id(messages) -> int:
    found = re.findall(r"insight_id[^0-9]{0,8}(\d+)", json.dumps(messages, default=str))
    assert found, "no insight id came back from write_insight"
    return int(found[-1])


def _write_proposal_on_the_unaccepted_insight(messages) -> ConversationTurn:
    return _calls(
        (
            "write_proposal",
            {
                "insight_id": _last_insight_id(messages),
                "action_code": "start_win_back",
                "angle": "whats_new",
                "reason": "Trying to push this straight through.",
            },
        )
    )


def _purge(session) -> None:
    run_ids = session.scalars(select(AgentRun.run_id).where(AgentRun.trigger == "chat")).all()
    insight_ids = session.scalars(
        select(AgentInsight.insight_id).where(AgentInsight.run_id.in_(run_ids))
    ).all()
    if insight_ids:
        session.execute(
            delete(AgentInsightFact).where(AgentInsightFact.insight_id.in_(insight_ids))
        )
    session.execute(delete(ChatTurn).where(ChatTurn.run_id.in_(run_ids)))
    session.execute(delete(ChatSession))
    if insight_ids:
        session.execute(delete(AgentProposal).where(AgentProposal.run_id.in_(run_ids)))
        session.execute(delete(AgentInsight).where(AgentInsight.insight_id.in_(insight_ids)))
    if run_ids:
        session.execute(delete(AgentEvent).where(AgentEvent.run_id.in_(run_ids)))
        session.execute(delete(AgentToolCall).where(AgentToolCall.run_id.in_(run_ids)))
        session.execute(delete(AuditLog).where(AuditLog.run_id.in_([str(r) for r in run_ids])))
        session.execute(delete(AgentRun).where(AgentRun.run_id.in_(run_ids)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.unit_fund_id == FUND_ID))
    session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.unit_fund_id == FUND_ID))
    session.commit()


@pytest.fixture(autouse=True)
async def _dispose_async_engine_after_each_test():
    yield
    await dispose_async_engine()


@pytest.fixture
def clean(db: None):
    with SessionLocal() as session:
        _purge(session)
    yield
    with SessionLocal() as session:
        _purge(session)


@pytest.fixture
def book(clean: None):
    with SessionLocal() as session:
        for client_id in CLIENT_IDS:
            session.add(
                ActiveClientFund(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    balance=600.0,
                    n_deposits=3,
                    n_withdrawals=0,
                    months_until_empty=2.0,
                )
            )
            session.add(
                ClientRiskFeatures(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    risk_band="High",
                    value_tier="Silver",
                    balance_tier="Small",
                    recency_band="Lapsed",
                    pattern_is_reliable=True,
                    overdue_multiple=2.0,
                    sig_heavy_withdrawal=False,
                    sig_dormant=True,
                    sig_broken_pattern=False,
                    sig_shrinking=False,
                    sig_going_dormant=False,
                    sig_never_repeated=False,
                    risk_score=70,
                    risk_reasons="dormant",
                    fund_at_risk=600.0,
                    config_version=1,
                )
            )
        session.commit()
    yield


async def _ask(question: str, turns: list) -> tuple[int, int, int]:
    with SessionLocal() as session:
        turn = start_chat_turn(session, question=question)
        await execute_chat_turn(session, turn, llm_client=ScriptedClient(turns), as_of=AS_OF)
        return turn.session_id, turn.turn_id, turn.run_id


async def test_a_read_question_is_answered_straight_away(book: None) -> None:
    _, turn_id, run_id = await _ask(
        "How many groups are on the watch list today?",
        [_final_answer("There are seven watch list groups today.")],
    )

    with SessionLocal() as session:
        run = session.get(AgentRun, run_id)
        turn = session.get(ChatTurn, turn_id)

    assert run.state == "completed"
    assert run.agent_kind == "chat"
    assert turn.answer == "There are seven watch list groups today."


async def test_a_request_to_act_only_writes_a_finding_for_review(book: None) -> None:
    _, turn_id, run_id = await _ask(
        "Please message the very small and quiet group about the fee.",
        [
            _calls(_write_insight()),
            _final_answer("I cannot send anything, so I have written it down for review."),
        ],
    )

    with SessionLocal() as session:
        insights = session.scalars(select(AgentInsight).where(AgentInsight.run_id == run_id)).all()
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.run_id == run_id)
        ).all()
        turn = session.get(ChatTurn, turn_id)

    assert len(insights) == 1
    assert insights[0].state == "new"
    assert not proposals
    assert "written it down" in turn.answer


async def test_the_chat_cannot_push_an_action_past_the_first_gate(book: None) -> None:
    _, _turn_id, run_id = await _ask(
        "Just send the win back campaign to that group now.",
        [
            _calls(_write_insight()),
            _write_proposal_on_the_unaccepted_insight,
            _final_answer("That cannot go ahead until a person accepts the finding."),
        ],
    )

    with SessionLocal() as session:
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.run_id == run_id)
        ).all()
        tool_calls = session.scalars(
            select(AgentToolCall)
            .where(AgentToolCall.run_id == run_id)
            .order_by(AgentToolCall.ordinal.asc())
        ).all()

    assert not proposals
    refusals = {
        call.tool_name: call.tool_output.get("error")
        for call in tool_calls
        if call.tool_output.get("error")
    }
    assert refusals.get("write_proposal") == "insight_not_accepted"


async def test_a_long_answer_records_each_step_in_order(book: None) -> None:
    _, _turn_id, run_id = await _ask(
        "What angles can we use and how big is the group?",
        [
            _calls(("list_angles", {})),
            _calls(
                (
                    "measure_slice",
                    {"conditions": [{"field": "risk_band", "op": "eq", "value": "High"}]},
                )
            ),
            _final_answer("Here is what I found across those two checks."),
        ],
    )

    with SessionLocal() as session:
        events = list_run_events(session, run_id, after=0, limit=500)

    kinds = [event.kind for event in events]
    assert kinds[0] == RUN_STARTED
    assert kinds[-1] == RUN_COMPLETED
    assert kinds.count(TOOL_STARTED) == 2
    assert kinds.count(TOOL_COMPLETED) == 2
    answer_ready = [
        event
        for event in events
        if event.kind == RUN_STATUS and event.detail.get("stage") == ANSWER_READY
    ]
    assert answer_ready
    assert answer_ready[0].detail["answer"] == "Here is what I found across those two checks."
    assert kinds.index(TOOL_STARTED) < kinds.index(RUN_COMPLETED)
