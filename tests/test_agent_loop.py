"""The nightly agent loop, against a fake model: a full run that ends in a
proposal, a run where a step raises and the run ends failed with no
proposal, and a run where the gates drop everyone the model chose.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import delete, select

from app.agents import agent_loop as agent_loop_module
from app.agents.agent_loop import (
    CHOOSE_ACTION_TOOL_NAME,
    NOT_SELECTED_TONIGHT,
    AgentRunInProgress,
    run_nightly_agent,
)
from app.agents.propose import DO_NOTHING_ACTION, ON_DO_NOT_CONTACT_LIST
from app.agents.watchlist import FEES_WILL_EMPTY, WatchlistThresholds
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.agent_run import AgentRun, AgentToolCall
from app.db.models.audit import AuditLog
from app.db.models.risk import RiskRun
from app.db.models.suppression import Suppression
from app.db.session import SessionLocal
from app.privacy.llm_client import ConversationTurn, LLMClientError, LLMUsage, ToolUseRequest

FUND_ID = 9610
ELIGIBLE_CLIENT = 961001
SUPPRESSED_CLIENT = 961002

AS_OF = date(2026, 9, 7)

TEST_RISK_RUN_ID = "todo15-test-risk-run"

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


class FakeTracer:
    """Records every span's input and output, instead of sending them anywhere."""

    def __init__(self) -> None:
        self.spans: list[dict] = []

    def start_span(
        self, *, trace_id, name, input, metadata=None, as_type="span", model=None, parent=None
    ):
        span = {
            "name": name,
            "input": input,
            "output": None,
            "as_type": as_type,
            "model": model,
            "parent": None if parent is None else parent["name"],
            "usage_details": None,
        }
        self.spans.append(span)
        return span

    def end_span(self, handle, *, output, usage_details=None, level=None, status_message=None):
        handle["output"] = output
        handle["usage_details"] = usage_details

    def get_trace_url(self, trace_id):
        return None

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class RaisingLLMClient:
    """A model client that always fails, to prove a failing step is recorded."""

    model = "fake-agent-model"

    def converse(self, *, system, messages, tools=()):
        raise LLMClientError("the model server is unreachable")


def _final_answer(text: str) -> ConversationTurn:
    return ConversationTurn(text=text, tool_requests=(), usage=_USAGE, stop_reason="end_turn")


def _choose_calls(entries: list[dict]) -> ConversationTurn:
    """One turn where the model calls choose_action once per entry."""
    return ConversationTurn(
        text="",
        tool_requests=tuple(
            ToolUseRequest(
                call_id=f"choose_{i}", tool_name=CHOOSE_ACTION_TOOL_NAME, tool_input=entry
            )
            for i, entry in enumerate(entries)
        ),
        usage=_USAGE,
        stop_reason="tool_use",
    )


def _choose_reply(entries: list[dict]) -> list[ConversationTurn]:
    """The turns a clean choose step takes: call choose_action, then stop."""
    return [_choose_calls(entries), _final_answer("Done.")]


def _stalling_choose_turn() -> ConversationTurn:
    """A choose_action call the tool always refuses, so the model never settles."""
    return _choose_calls(
        [
            {
                "group_name": FEES_WILL_EMPTY,
                "action_code": "not_a_real_action",
                "angle": None,
                "reason": "trying",
            }
        ]
    )


def _purge(session) -> None:
    client_ids = (ELIGIBLE_CLIENT, SUPPRESSED_CLIENT)
    run_ids = session.scalars(select(AgentRun.run_id).where(AgentRun.trigger == "manual")).all()
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
    session.execute(delete(Suppression).where(Suppression.client_id.in_(client_ids)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
    session.execute(delete(RiskRun).where(RiskRun.run_id == TEST_RISK_RUN_ID))
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
    monkeypatch.setattr(agent_loop_module, "load_thresholds", lambda session, as_of: THRESHOLDS)


def _seed_fees_will_empty_client(client_id: int) -> None:
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


def test_a_full_run_produces_a_proposal_chosen_by_the_model() -> None:
    _seed_fees_will_empty_client(ELIGIBLE_CLIENT)
    llm_client = FakeConversingLLMClient(
        [
            _final_answer("The fee warning group matters tonight. Nothing else does."),
            *_choose_reply(
                [
                    {
                        "group_name": FEES_WILL_EMPTY,
                        "action_code": "fee_warning",
                        "angle": "sitting_still",
                        "reason": "their balance will run out within a few months",
                    }
                ]
            ),
        ]
    )

    with SessionLocal() as session:
        run = run_nightly_agent(session, trigger="manual", llm_client=llm_client, as_of=AS_OF)
        run_id = run.run_id

    with SessionLocal() as session:
        stored_run = session.get(AgentRun, run_id)
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.run_id == run_id)
        ).all()
        audit_actions = session.scalars(
            select(AuditLog.action).where(AuditLog.run_id == str(run_id))
        ).all()

    assert stored_run.state == "completed"
    assert stored_run.failure_reason is None
    assert stored_run.plan_text
    assert stored_run.summary
    assert {"gather", "plan", "choose", "check", "propose", "report"} <= set(audit_actions)

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.group_name == FEES_WILL_EMPTY
    assert proposal.action_code == "fee_warning"
    assert proposal.angle == "sitting_still"
    assert proposal.reason == "their balance will run out within a few months"

    with SessionLocal() as session:
        clients = session.scalars(
            select(AgentProposalClient).where(
                AgentProposalClient.proposal_id == proposal.proposal_id
            )
        ).all()
    assert len(clients) == 1
    assert clients[0].client_id == ELIGIBLE_CLIENT
    assert clients[0].included is True
    assert clients[0].skip_reason is None


def test_every_step_traces_a_real_input_and_output_without_a_client_id() -> None:
    _seed_fees_will_empty_client(ELIGIBLE_CLIENT)
    llm_client = FakeConversingLLMClient(
        [
            _final_answer("The fee warning group matters tonight. Nothing else does."),
            *_choose_reply(
                [
                    {
                        "group_name": FEES_WILL_EMPTY,
                        "action_code": "fee_warning",
                        "angle": "sitting_still",
                        "reason": "their balance will run out within a few months",
                    }
                ]
            ),
        ]
    )
    tracer = FakeTracer()

    with SessionLocal() as session:
        run_nightly_agent(
            session, trigger="manual", llm_client=llm_client, as_of=AS_OF, tracer=tracer
        )

    steps = [span for span in tracer.spans if span["parent"] is None]
    assert [span["name"] for span in steps] == [
        "gather",
        "plan",
        "choose",
        "check",
        "propose",
        "report",
    ]
    for span in steps:
        if span["name"] != "gather":
            assert span["input"], f"{span['name']} span had no input"
        assert span["output"], f"{span['name']} span had no output"
    for span in tracer.spans:
        rendered = json.dumps(span["input"], default=str) + json.dumps(span["output"], default=str)
        assert str(ELIGIBLE_CLIENT) not in rendered
        assert str(FUND_ID) not in rendered

    model_calls = [span for span in tracer.spans if span["as_type"] == "generation"]
    assert model_calls
    assert {span["parent"] for span in model_calls} == {"plan", "choose"}
    for span in model_calls:
        assert span["model"] == "fake-agent-model"
        assert span["usage_details"] == {
            "input": _USAGE.input_tokens,
            "output": _USAGE.output_tokens,
        }


def test_a_failing_step_leaves_the_run_failed_and_writes_no_proposal() -> None:
    _seed_fees_will_empty_client(ELIGIBLE_CLIENT)
    llm_client = RaisingLLMClient()

    with SessionLocal() as session:
        run = run_nightly_agent(session, trigger="manual", llm_client=llm_client, as_of=AS_OF)
        run_id = run.run_id

    with SessionLocal() as session:
        stored_run = session.get(AgentRun, run_id)
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.run_id == run_id)
        ).all()
        audit_actions = session.scalars(
            select(AuditLog.action).where(AuditLog.run_id == str(run_id))
        ).all()

    assert stored_run.state == "failed"
    assert stored_run.failure_reason is not None
    assert "unreachable" in stored_run.failure_reason
    assert proposals == []
    assert "gather" in audit_actions
    assert "failed" in audit_actions
    assert "propose" not in audit_actions
    assert "report" not in audit_actions


def test_a_run_where_the_gates_drop_everyone_still_completes_with_do_nothing() -> None:
    _seed_fees_will_empty_client(SUPPRESSED_CLIENT)
    with SessionLocal() as session:
        session.add(Suppression(client_id=SUPPRESSED_CLIENT, reason="asked not to be contacted"))
        session.commit()

    llm_client = FakeConversingLLMClient(
        [
            _final_answer("The fee warning group looks like it matters tonight."),
            *_choose_reply(
                [
                    {
                        "group_name": FEES_WILL_EMPTY,
                        "action_code": "fee_warning",
                        "angle": "sitting_still",
                        "reason": "their balance will run out within a few months",
                    }
                ]
            ),
        ]
    )

    with SessionLocal() as session:
        run = run_nightly_agent(session, trigger="manual", llm_client=llm_client, as_of=AS_OF)
        run_id = run.run_id

    with SessionLocal() as session:
        stored_run = session.get(AgentRun, run_id)
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.run_id == run_id)
        ).all()

    assert stored_run.state == "completed"
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.action_code == DO_NOTHING_ACTION
    assert proposal.money_total_kes == 0
    assert proposal.skip_reason_counts == {ON_DO_NOT_CONTACT_LIST: 1}

    with SessionLocal() as session:
        clients = session.scalars(
            select(AgentProposalClient).where(
                AgentProposalClient.proposal_id == proposal.proposal_id
            )
        ).all()
    assert len(clients) == 1
    assert clients[0].included is False
    assert clients[0].skip_reason == ON_DO_NOT_CONTACT_LIST


def test_a_wrong_action_is_corrected_on_the_next_turn() -> None:
    _seed_fees_will_empty_client(ELIGIBLE_CLIENT)
    llm_client = FakeConversingLLMClient(
        [
            _final_answer("The fee warning group matters tonight. Nothing else does."),
            _choose_calls(
                [
                    {
                        "group_name": FEES_WILL_EMPTY,
                        "action_code": "not_a_real_action",
                        "angle": None,
                        "reason": "their balance will run out within a few months",
                    }
                ]
            ),
            _choose_calls(
                [
                    {
                        "group_name": FEES_WILL_EMPTY,
                        "action_code": "fee_warning",
                        "angle": "sitting_still",
                        "reason": "their balance will run out within a few months",
                    }
                ]
            ),
            _final_answer("Done."),
        ]
    )

    with SessionLocal() as session:
        run = run_nightly_agent(session, trigger="manual", llm_client=llm_client, as_of=AS_OF)
        run_id = run.run_id

    with SessionLocal() as session:
        stored_run = session.get(AgentRun, run_id)
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.run_id == run_id)
        ).all()
        choose_audit = session.scalar(
            select(AuditLog).where(AuditLog.run_id == str(run_id), AuditLog.action == "choose")
        )

    assert stored_run.state == "completed"
    assert len(proposals) == 1
    assert proposals[0].action_code == "fee_warning"
    assert choose_audit.detail["fell_back"] is False
    assert choose_audit.detail["last_error"] is None
    assert choose_audit.detail["attempts"] == 1


def test_the_turn_cap_falls_back_to_do_nothing_and_records_the_last_refusal() -> None:
    _seed_fees_will_empty_client(ELIGIBLE_CLIENT)
    llm_client = FakeConversingLLMClient(
        [
            _final_answer("The fee warning group matters tonight."),
            *([_stalling_choose_turn()] * 6),
        ]
    )

    with SessionLocal() as session:
        run = run_nightly_agent(
            session,
            trigger="manual",
            llm_client=llm_client,
            as_of=AS_OF,
            max_choose_attempts=1,
        )
        run_id = run.run_id

    with SessionLocal() as session:
        stored_run = session.get(AgentRun, run_id)
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.run_id == run_id)
        ).all()
        choose_audit = session.scalar(
            select(AuditLog).where(AuditLog.run_id == str(run_id), AuditLog.action == "choose")
        )

    assert stored_run.state == "completed"
    assert len(proposals) == 1
    assert proposals[0].action_code == DO_NOTHING_ACTION
    assert proposals[0].skip_reason_counts == {NOT_SELECTED_TONIGHT: 1}
    assert choose_audit.detail["fell_back"] is True
    assert choose_audit.detail["attempts"] == 1
    assert "not_a_real_action" in choose_audit.detail["last_error"]


def test_a_run_works_with_no_risk_run_on_file(monkeypatch) -> None:
    monkeypatch.setattr(agent_loop_module, "_latest_completed_risk_run_id", lambda session: None)

    with SessionLocal() as session:
        run = run_nightly_agent(
            session,
            trigger="manual",
            llm_client=FakeConversingLLMClient([_final_answer("Quiet night.")]),
            as_of=AS_OF,
        )
        run_id = run.run_id

    with SessionLocal() as session:
        stored_run = session.get(AgentRun, run_id)

    assert stored_run.risk_run_id is None
    assert stored_run.state == "completed"


def test_a_run_records_which_risk_run_it_read(monkeypatch) -> None:
    with SessionLocal() as session:
        session.add(RiskRun(run_id=TEST_RISK_RUN_ID, state="completed", config_version=1))
        session.commit()
    monkeypatch.setattr(
        agent_loop_module, "_latest_completed_risk_run_id", lambda session: TEST_RISK_RUN_ID
    )

    with SessionLocal() as session:
        run = run_nightly_agent(
            session,
            trigger="manual",
            llm_client=FakeConversingLLMClient([_final_answer("Quiet night.")]),
            as_of=AS_OF,
        )
        run_id = run.run_id

    with SessionLocal() as session:
        stored_run = session.get(AgentRun, run_id)

    assert stored_run.risk_run_id == TEST_RISK_RUN_ID


def test_a_second_start_is_refused_while_one_is_running() -> None:
    with SessionLocal() as session:
        session.add(AgentRun(trigger="manual", state="running"))
        session.commit()

        with pytest.raises(AgentRunInProgress):
            agent_loop_module.start_agent_run(session, trigger="manual", as_of=AS_OF)

        still_one_running = session.scalars(
            select(AgentRun).where(AgentRun.state == "running", AgentRun.trigger == "manual")
        ).all()
    assert len(still_one_running) == 1
