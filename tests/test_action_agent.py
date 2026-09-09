"""The action agent, against a fake model: an accepted finding becomes a
proposal that names it, a dismissed one never does, the checks drop a client
who may not be contacted, and what the finding says to avoid reaches the
drafting prompt.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete, select

from app.agents import insight_members as insight_members_module
from app.agents.action_agent import (
    CHOOSE_RESPONSE_TOOL_NAME,
    InsightNotActionable,
    run_action_agent,
)
from app.agents.action_brief import insight_prohibitions
from app.agents.email_agent import build_system_prompt
from app.agents.propose import DO_NOTHING_ACTION, ON_DO_NOT_CONTACT_LIST
from app.agents.watchlist import FEES_WILL_EMPTY, WatchlistThresholds
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.agent_run import AgentRun, AgentToolCall
from app.db.models.audit import AuditLog
from app.db.models.suppression import Suppression
from app.db.session import SessionLocal
from app.privacy.llm_client import ConversationTurn, LLMUsage, ToolUseRequest

FUND_ID = 9741
ELIGIBLE_CLIENT = 974101
SUPPRESSED_CLIENT = 974102

AS_OF = date(2026, 9, 9)

INSIGHT_TITLE = "A group whose balance the monthly fee will finish"

AVOID_SAYING = "that they are about to leave, or that their account is nearly empty"

THRESHOLDS = WatchlistThresholds(
    new_client_days=30,
    months_until_empty=6.0,
    small_balance=100.0,
    awaiting_call_days=2,
)

_USAGE = LLMUsage(input_tokens=10, output_tokens=10)


class FakeConversingLLMClient:
    """Pops one scripted turn per converse() call, in the order given."""

    model = "fake-agent-model"

    def __init__(self, turns: list[ConversationTurn]) -> None:
        self._turns = list(turns)

    def converse(self, *, system, messages, tools=()):
        if not self._turns:
            raise AssertionError("the fake model was asked for more turns than scripted")
        return self._turns.pop(0)


def _final_answer(text: str) -> ConversationTurn:
    return ConversationTurn(text=text, tool_requests=(), usage=_USAGE, stop_reason="end_turn")


def _choose_reply(tool_input: dict) -> list[ConversationTurn]:
    """The turns a clean choose step takes: choose_response, then stop."""
    return [
        ConversationTurn(
            text="",
            tool_requests=(
                ToolUseRequest(
                    call_id="choose_0",
                    tool_name=CHOOSE_RESPONSE_TOOL_NAME,
                    tool_input=tool_input,
                ),
            ),
            usage=_USAGE,
            stop_reason="tool_use",
        ),
        _final_answer("Done."),
    ]


def _fee_warning_reply() -> list[ConversationTurn]:
    return _choose_reply(
        {
            "action_code": "fee_warning",
            "angle": "sitting_still",
            "reason": "their balance runs out within a few months at the current fee",
        }
    )


def _purge(session) -> None:
    client_ids = (ELIGIBLE_CLIENT, SUPPRESSED_CLIENT)
    run_ids = session.scalars(select(AgentRun.run_id).where(AgentRun.agent_kind == "action")).all()
    proposal_ids = session.scalars(
        select(AgentProposal.proposal_id).where(AgentProposal.run_id.in_(run_ids))
    ).all()
    if proposal_ids:
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "agent_proposal",
                AuditLog.entity_id.in_([str(pid) for pid in proposal_ids]),
            )
        )
        session.execute(
            delete(AgentProposalClient).where(AgentProposalClient.proposal_id.in_(proposal_ids))
        )
        session.execute(delete(AgentProposal).where(AgentProposal.proposal_id.in_(proposal_ids)))
    if run_ids:
        session.execute(delete(AuditLog).where(AuditLog.run_id.in_([str(r) for r in run_ids])))
        session.execute(delete(AgentToolCall).where(AgentToolCall.run_id.in_(run_ids)))
        session.execute(delete(AgentRun).where(AgentRun.run_id.in_(run_ids)))

    insight_ids = session.scalars(
        select(AgentInsight.insight_id).where(AgentInsight.title == INSIGHT_TITLE)
    ).all()
    if insight_ids:
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "agent_insight",
                AuditLog.entity_id.in_([str(i) for i in insight_ids]),
            )
        )
        session.execute(delete(AgentInsight).where(AgentInsight.insight_id.in_(insight_ids)))
    session.execute(delete(Suppression).where(Suppression.client_id.in_(client_ids)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
    session.commit()


@pytest.fixture
def clean(db: None):
    with SessionLocal() as session:
        _purge(session)
    yield
    with SessionLocal() as session:
        _purge(session)


@pytest.fixture(autouse=True)
def _fixed_thresholds(monkeypatch, clean: None):
    monkeypatch.setattr(
        insight_members_module, "load_thresholds", lambda session, as_of: THRESHOLDS
    )


def _seed_client(client_id: int) -> None:
    with SessionLocal() as session:
        session.add(
            ActiveClientFund(
                client_id=client_id,
                unit_fund_id=FUND_ID,
                balance=200_000.0,
                n_deposits=5,
                n_withdrawals=0,
                months_until_empty=2.0,
            )
        )
        session.commit()


def _write_insight(*, state: str, group_name: str = FEES_WILL_EMPTY) -> int:
    with SessionLocal() as session:
        insight = AgentInsight(
            kind="risk",
            title=INSIGHT_TITLE,
            group_name=group_name,
            client_count=2,
            money_total_kes=400_000.0,
            confidence="high",
            confidence_reason="every client in it was counted the same way twice",
            suggestion="tell them before the balance runs out",
            avoid_saying=AVOID_SAYING,
            why_now="the fee takes another month off every one of them",
            state=state,
            dismissed_reason="not worth acting on" if state == "dismissed" else None,
        )
        session.add(insight)
        session.commit()
        return insight.insight_id


def test_an_accepted_finding_becomes_a_proposal_that_names_it() -> None:
    _seed_client(ELIGIBLE_CLIENT)
    insight_id = _write_insight(state="accepted")
    llm_client = FakeConversingLLMClient(_fee_warning_reply())

    with SessionLocal() as session:
        run = run_action_agent(session, insight_id, llm_client=llm_client, as_of=AS_OF)
        run_id = run.run_id

    with SessionLocal() as session:
        stored_run = session.get(AgentRun, run_id)
        insight = session.get(AgentInsight, insight_id)
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.run_id == run_id)
        ).all()
        audit_actions = session.scalars(
            select(AuditLog.action).where(AuditLog.run_id == str(run_id))
        ).all()

    assert stored_run.state == "completed"
    assert stored_run.agent_kind == "action"
    assert stored_run.insight_id == insight_id
    assert stored_run.summary
    assert {"read", "choose", "check", "propose", "report"} <= set(audit_actions)

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.insight_id == insight_id
    assert proposal.action_code == "fee_warning"
    assert proposal.angle == "sitting_still"
    assert proposal.status == "proposed"
    assert insight.state == "acted_on"

    with SessionLocal() as session:
        clients = session.scalars(
            select(AgentProposalClient).where(
                AgentProposalClient.proposal_id == proposal.proposal_id
            )
        ).all()
    assert [(row.client_id, row.included) for row in clients] == [(ELIGIBLE_CLIENT, True)]


def test_a_client_who_may_not_be_contacted_is_dropped_with_the_reason() -> None:
    _seed_client(ELIGIBLE_CLIENT)
    _seed_client(SUPPRESSED_CLIENT)
    with SessionLocal() as session:
        session.add(Suppression(client_id=SUPPRESSED_CLIENT, reason="asked us to stop"))
        session.commit()

    insight_id = _write_insight(state="accepted")
    llm_client = FakeConversingLLMClient(_fee_warning_reply())

    with SessionLocal() as session:
        run = run_action_agent(session, insight_id, llm_client=llm_client, as_of=AS_OF)
        run_id = run.run_id

    with SessionLocal() as session:
        proposal = session.scalars(
            select(AgentProposal).where(AgentProposal.run_id == run_id)
        ).one()
        clients = session.scalars(
            select(AgentProposalClient)
            .where(AgentProposalClient.proposal_id == proposal.proposal_id)
            .order_by(AgentProposalClient.client_id)
        ).all()

    assert proposal.skip_reason_counts == {ON_DO_NOT_CONTACT_LIST: 1}
    assert [(row.client_id, row.included, row.skip_reason) for row in clients] == [
        (ELIGIBLE_CLIENT, True, None),
        (SUPPRESSED_CLIENT, False, ON_DO_NOT_CONTACT_LIST),
    ]


def test_a_dismissed_finding_never_becomes_a_proposal() -> None:
    _seed_client(ELIGIBLE_CLIENT)
    insight_id = _write_insight(state="dismissed")
    llm_client = FakeConversingLLMClient(_fee_warning_reply())

    with SessionLocal() as session:
        with pytest.raises(InsightNotActionable):
            run_action_agent(session, insight_id, llm_client=llm_client, as_of=AS_OF)

    with SessionLocal() as session:
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.insight_id == insight_id)
        ).all()
        runs = session.scalars(select(AgentRun).where(AgentRun.insight_id == insight_id)).all()

    assert proposals == []
    assert runs == []


def test_a_finding_with_no_group_and_no_filter_proposes_nothing() -> None:
    _seed_client(ELIGIBLE_CLIENT)
    insight_id = _write_insight(state="accepted", group_name=FEES_WILL_EMPTY)
    with SessionLocal() as session:
        session.get(AgentInsight, insight_id).group_name = "a group nobody defined"
        session.commit()

    llm_client = FakeConversingLLMClient(_fee_warning_reply())
    with SessionLocal() as session:
        run = run_action_agent(session, insight_id, llm_client=llm_client, as_of=AS_OF)
        run_id = run.run_id

    with SessionLocal() as session:
        proposal = session.scalars(
            select(AgentProposal).where(AgentProposal.run_id == run_id)
        ).one()
        clients = session.scalars(
            select(AgentProposalClient).where(
                AgentProposalClient.proposal_id == proposal.proposal_id
            )
        ).all()

    assert proposal.action_code == DO_NOTHING_ACTION
    assert proposal.insight_id == insight_id
    assert clients == []


def test_what_the_finding_says_to_avoid_reaches_the_drafting_prompt() -> None:
    insight_id = _write_insight(state="accepted")
    with SessionLocal() as session:
        insight = session.get(AgentInsight, insight_id)
        lines = insight_prohibitions(insight)

    assert any(AVOID_SAYING in line for line in lines)

    prompt = build_system_prompt(
        angle="sitting_still", prompt_variant=None, extra_prohibitions=lines
    )
    assert AVOID_SAYING in prompt
    for line in lines:
        assert f"- {line}" in prompt
