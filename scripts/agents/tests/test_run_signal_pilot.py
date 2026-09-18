"""Covers run_signal_pilot.py's underlying functions, not the CLI wrapper:
a run that finds new clients, a run that finds none, and a second run on
unchanged data that must not write a second proposal.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.agents.situations import NEW_CLIENT_SINGLE_DEPOSIT
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.audit import AuditLog
from app.db.models.campaigns import Enrollment, TouchLog
from app.db.models.llmops import (
    Evaluation,
    GenerationRun,
    LLMRequest,
    LLMResponse,
    TokenUsage,
    ToolCall,
    TraceRef,
)
from app.db.models.outreach import OutreachMessage, ReviewAction
from app.db.models.signals import (
    ClientSignalSnapshot,
    ClientSignalState,
    ClientSituationSnapshot,
    ClientSituationState,
)
from app.db.session import SessionLocal

_MODULE_PATH = Path(__file__).resolve().parent.parent / "run_signal_pilot.py"


def _load_pilot_module():
    spec = importlib.util.spec_from_file_location("scripts_agents_run_signal_pilot", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FUND_ID = 9522
NEW_CLIENT = 952201
OUT_OF_WINDOW_CLIENT = 952202

CLIENT_IDS = (NEW_CLIENT, OUT_OF_WINDOW_CLIENT)

AS_OF = date(2026, 9, 15)


@pytest.fixture(autouse=True)
def fixed_window(monkeypatch):
    from app.agents import signals, situation_action_mapping, write_tools

    monkeypatch.setattr(signals, "active_threshold", lambda *args, **kwargs: 30)
    monkeypatch.setattr(write_tools, "draft_into_review_queue", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        situation_action_mapping,
        "action_code_for_situation",
        lambda *args, **kwargs: "welcome_and_top_up",
    )


def _fund(client_id: int, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance=250_000.0,
        n_deposits=1,
        n_withdrawals=0,
        first_deposit_date=AS_OF - timedelta(days=3),
    )
    row.update(overrides)
    return ActiveClientFund(**row)


def _purge_book(session) -> None:
    stale_proposal_ids = session.scalars(
        select(AgentProposalClient.proposal_id).where(AgentProposalClient.client_id.in_(CLIENT_IDS))
    ).all()
    _purge_proposals(session, stale_proposal_ids)
    session.execute(
        delete(ClientSituationSnapshot).where(ClientSituationSnapshot.client_id.in_(CLIENT_IDS))
    )
    session.execute(
        delete(ClientSituationState).where(ClientSituationState.client_id.in_(CLIENT_IDS))
    )
    session.execute(
        delete(ClientSignalSnapshot).where(ClientSignalSnapshot.client_id.in_(CLIENT_IDS))
    )
    session.execute(delete(ClientSignalState).where(ClientSignalState.client_id.in_(CLIENT_IDS)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(CLIENT_IDS)))
    session.commit()


def _purge_proposals(session, proposal_ids: list[int]) -> None:
    message_ids = session.scalars(
        select(OutreachMessage.message_id).where(OutreachMessage.client_id.in_(CLIENT_IDS))
    ).all()
    if message_ids:
        session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
        session.execute(delete(TouchLog).where(TouchLog.message_id.in_(message_ids)))
    enrollment_ids = session.scalars(
        select(Enrollment.enrollment_id).where(Enrollment.client_id.in_(CLIENT_IDS))
    ).all()
    if enrollment_ids:
        session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
    session.execute(delete(OutreachMessage).where(OutreachMessage.client_id.in_(CLIENT_IDS)))
    session.execute(delete(Enrollment).where(Enrollment.client_id.in_(CLIENT_IDS)))

    run_ids = session.scalars(
        select(GenerationRun.run_id).where(GenerationRun.client_id.in_(CLIENT_IDS))
    ).all()
    if run_ids:
        request_ids = session.scalars(
            select(LLMRequest.request_id).where(LLMRequest.run_id.in_(run_ids))
        ).all()
        if request_ids:
            session.execute(delete(TokenUsage).where(TokenUsage.request_id.in_(request_ids)))
            session.execute(delete(LLMResponse).where(LLMResponse.request_id.in_(request_ids)))
            session.execute(delete(LLMRequest).where(LLMRequest.run_id.in_(run_ids)))
        session.execute(delete(ToolCall).where(ToolCall.run_id.in_(run_ids)))
        session.execute(delete(TraceRef).where(TraceRef.run_id.in_(run_ids)))
        session.execute(delete(Evaluation).where(Evaluation.run_id.in_(run_ids)))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id.in_(run_ids)))

    if not proposal_ids:
        session.commit()
        return
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
    session.commit()


def _purge_insights(session, insight_ids: list[int]) -> None:
    if not insight_ids:
        return
    session.execute(delete(AgentInsight).where(AgentInsight.insight_id.in_(insight_ids)))
    session.commit()


@pytest.fixture
def made(db: None):
    """Ids the test creates, cleaned up by exact id rather than by name, so
    this never touches another run's proposals or findings for the same
    watch list group.
    """
    created: dict[str, list[int]] = {"insight_ids": [], "proposal_ids": []}

    with SessionLocal() as session:
        _purge_book(session)

    yield created

    with SessionLocal() as session:
        _purge_proposals(session, created["proposal_ids"])
        _purge_insights(session, created["insight_ids"])
        _purge_book(session)


def _remember(created: dict[str, list[int]], result) -> None:
    for sit_res in result.situations:
        if sit_res.insight is not None:
            created["insight_ids"].append(sit_res.insight.insight_id)
        if sit_res.proposal is not None:
            created["proposal_ids"].append(sit_res.proposal.proposal_id)


def _find_situation(result, code: str):
    return next((s for s in result.situations if s.situation_code == code), None)


@pytest.fixture
def book(made: dict[str, list[int]]):
    with SessionLocal() as session:
        session.add(_fund(NEW_CLIENT))
        session.commit()
    return made


@pytest.fixture
def empty_book(made: dict[str, list[int]]):
    with SessionLocal() as session:
        session.add(_fund(OUT_OF_WINDOW_CLIENT, first_deposit_date=AS_OF - timedelta(days=90)))
        session.commit()
    return made


def test_a_run_that_finds_new_clients_proposes_and_runs_it(
    book: dict[str, list[int]],
) -> None:
    pilot = _load_pilot_module()
    with SessionLocal() as session:
        result = pilot.run_pilot(session, AS_OF, reviewer="test-reviewer", max_clients=5, limit=3)
    _remember(book, result)

    sit_res = _find_situation(result, NEW_CLIENT_SINGLE_DEPOSIT)
    assert sit_res is not None
    assert len(sit_res.delta.newly_active) >= 1
    assert sit_res.insight is not None
    assert sit_res.insight.client_count >= 1
    assert sit_res.proposal is not None
    assert sit_res.proposal.status == "running"
    assert sit_res.reused_existing_proposal is False
    assert sit_res.stopped_reason is None


def test_a_run_that_finds_none_still_recomputes_cleanly(
    empty_book: dict[str, list[int]],
) -> None:
    pilot = _load_pilot_module()
    with SessionLocal() as session:
        result = pilot.run_pilot(session, AS_OF, reviewer="test-reviewer", max_clients=5, limit=3)
    _remember(empty_book, result)

    sit_res = _find_situation(result, NEW_CLIENT_SINGLE_DEPOSIT)
    assert sit_res.delta.newly_active == ()
    assert sit_res.insight is None
    assert sit_res.proposal is None
    assert sit_res.stopped_reason is not None


def test_a_second_run_the_same_day_does_not_write_a_second_proposal(
    book: dict[str, list[int]],
) -> None:
    pilot = _load_pilot_module()
    with SessionLocal() as session:
        first = pilot.run_pilot(session, AS_OF, reviewer="test-reviewer", max_clients=5, limit=3)
    _remember(book, first)

    with SessionLocal() as session:
        second = pilot.run_pilot(session, AS_OF, reviewer="test-reviewer", max_clients=5, limit=3)
    _remember(book, second)

    first_sit = _find_situation(first, NEW_CLIENT_SINGLE_DEPOSIT)
    second_sit = _find_situation(second, NEW_CLIENT_SINGLE_DEPOSIT)

    assert first_sit.proposal is not None
    assert second_sit.proposal is None
    assert second_sit.delta.newly_active == ()
    assert second_sit.stopped_reason is not None
    assert len(set(book["proposal_ids"])) == 1
