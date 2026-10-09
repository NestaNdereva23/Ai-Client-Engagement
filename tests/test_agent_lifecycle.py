from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from test_agent_intelligence import AS_OF, THRESHOLDS, ScriptedAsyncClient, _calls, _final_answer

from app.agents import intelligence as intelligence_module
from app.agents import lifecycle_tools as lifecycle_tools_module
from app.agents.action_agent import InsightNotActionable, start_action_run
from app.agents.insight_tools import WRITE_INSIGHT, make_insight_tools
from app.agents.intelligence import investigation_tool_specs, run_intelligence_agent
from app.agents.lifecycle_policy import PolicyRefused, set_policy
from app.agents.lifecycle_rules import run_lifecycle_rules
from app.agents.lifecycle_tools import WRITE_LIFECYCLE_CHANGE, make_lifecycle_tools
from app.agents.watchlist import VERY_SMALL_AND_QUIET
from app.config import get_settings
from app.db.async_session import dispose_async_engine
from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_run import AgentRun, AgentToolCall
from app.db.models.audit import AuditLog
from app.db.models.client_lifecycle import (
    AUTOMATIC,
    NEEDS_A_PERSON,
    AgentInsightLifecycle,
    ClientLifecycle,
    LifecyclePolicy,
)
from app.db.models.risk import ClientRiskFeatures
from app.db.session import SessionLocal
from app.main import app
from app.services.agent_insights import decide_insight

client = TestClient(app)

INSIGHTS = "/api/v1/agent/insights"

FUND_ID = 99780
CLIENT_IDS = (997801, 997802, 997803, 997804)
MEETING_EVIDENCE = CLIENT_IDS[:3]

EVERYONE = [{"field": "fund_at_risk", "op": "eq", "value": 4321.0}]
DORMANT_EVIDENCE = [{"field": "sig_dormant", "op": "is_true"}]


@pytest.fixture(autouse=True)
async def _dispose_async_engine_after_each_test():
    yield
    await dispose_async_engine()


def _purge(session, run_id: int | None = None) -> None:
    if run_id is not None:
        session.execute(delete(AgentToolCall).where(AgentToolCall.run_id == run_id))
        session.execute(delete(AuditLog).where(AuditLog.run_id == str(run_id)))
    session.execute(delete(ClientLifecycle).where(ClientLifecycle.unit_fund_id == FUND_ID))
    session.execute(
        delete(ActiveClientInteraction).where(ActiveClientInteraction.unit_fund_id == FUND_ID)
    )
    session.execute(
        delete(AuditLog).where(
            AuditLog.entity_type == "client_lifecycle", AuditLog.entity_id.like(f"%/{FUND_ID}")
        )
    )
    session.execute(
        delete(AuditLog).where(
            AuditLog.entity_type == "lifecycle_policy", AuditLog.actor_id == "tester"
        )
    )
    session.execute(
        delete(LifecyclePolicy).where(LifecyclePolicy.policy_code.in_(("test_dormant_to_active",)))
    )
    if run_id is not None:
        session.execute(delete(AgentInsight).where(AgentInsight.run_id == run_id))
        session.execute(delete(AgentRun).where(AgentRun.run_id == run_id))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.unit_fund_id == FUND_ID))
    session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.unit_fund_id == FUND_ID))
    session.commit()


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session)
        run = AgentRun(trigger="manual", state="completed")
        session.add(run)
        session.flush()
        for index, client_id in enumerate(CLIENT_IDS):
            session.add(
                ClientRiskFeatures(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    risk_band="High",
                    value_tier="Gold",
                    balance_tier="Small",
                    recency_band="Lapsed",
                    pattern_is_reliable=True,
                    overdue_multiple=1.0,
                    sig_heavy_withdrawal=False,
                    sig_dormant=index < len(MEETING_EVIDENCE),
                    sig_broken_pattern=False,
                    sig_shrinking=False,
                    sig_going_dormant=False,
                    sig_never_repeated=False,
                    risk_score=50,
                    risk_reasons="dormant",
                    fund_at_risk=4_321.0,
                    config_version=1,
                )
            )
            session.add(
                ActiveClientFund(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    balance=20_000.0 + index,
                    n_deposits=3,
                    n_withdrawals=0,
                )
            )
        session.commit()
        run_id = run.run_id

    yield run_id

    with SessionLocal() as session:
        _purge(session, run_id)
        session.execute(
            delete(LifecyclePolicy).where(
                LifecyclePolicy.from_state == "active", LifecyclePolicy.to_state == "dormant"
            )
        )
        session.commit()


def _policy(session, mode: str | None) -> None:
    session.execute(
        delete(LifecyclePolicy).where(
            LifecyclePolicy.from_state == "active", LifecyclePolicy.to_state == "dormant"
        )
    )
    if mode is not None:
        set_policy(
            session,
            policy_code="active_to_dormant",
            from_state="active",
            to_state="dormant",
            evidence=DORMANT_EVIDENCE,
            mode=mode,
            description="no deposit in 12 months",
            changed_by="tester",
            changed_reason="test",
        )
    session.commit()


def _raise_it(session, run_id: int, **overrides) -> dict:
    arguments = {
        "from_state": "active",
        "to_state": "dormant",
        "title": "Clients who stopped paying in still labelled active",
        "why_now": "Their last deposit was over a year ago",
        "confidence": "high",
        "confidence_reason": "Every one of them shows the same gap",
        "conditions": EVERYONE,
    }
    arguments.update(overrides)
    return make_lifecycle_tools(run_id=run_id)[WRITE_LIFECYCLE_CHANGE](session, **arguments)


def _changed(session) -> dict[int, ClientLifecycle]:
    rows = session.scalars(select(ClientLifecycle).where(ClientLifecycle.unit_fund_id == FUND_ID))
    return {row.client_id: row for row in rows}


def test_a_rule_that_acts_alone_moves_only_the_clients_that_meet_its_evidence(book: int) -> None:
    with SessionLocal() as session:
        _policy(session, AUTOMATIC)
        written = _raise_it(session, book)
        session.commit()
        insight_id = written["insight_id"]

        assert written["status"] == "written"
        assert written["client_funds"] == len(CLIENT_IDS)
        assert _changed(session) == {}

        outcomes = run_lifecycle_rules(session, run_id=book)
        session.commit()

        assert outcomes == {"applied": 1}
        changed = _changed(session)
        assert set(changed) == set(MEETING_EVIDENCE)
        for row in changed.values():
            assert row.state == "dormant"
            assert row.policy_code == "active_to_dormant"
            assert row.workflow == "dormant_follow_up"
            assert row.changed_by == "lifecycle_rules"
            assert row.insight_id == insight_id
        assert session.get(AgentInsight, insight_id).state == "acted_on"
        assert session.get(AgentInsightLifecycle, insight_id).outcome == "applied"

        trail = session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "client_lifecycle", AuditLog.entity_id.like(f"%/{FUND_ID}")
            )
        ).all()
        assert len(trail) == len(MEETING_EVIDENCE)
        assert {row.detail["policy_code"] for row in trail} == {"active_to_dormant"}
        assert {row.detail["insight_id"] for row in trail} == {insight_id}
        noticed = session.scalar(
            select(AuditLog).where(
                AuditLog.entity_type == "agent_insight",
                AuditLog.entity_id == str(insight_id),
                AuditLog.action == "write",
            )
        )
        assert noticed is not None

        flags = session.scalars(
            select(ActiveClientInteraction).where(ActiveClientInteraction.unit_fund_id == FUND_ID)
        ).all()
        assert {flag.client_id for flag in flags} == set(MEETING_EVIDENCE)
        assert {flag.reviewer_id for flag in flags} == {"lifecycle_rules"}


def test_a_rule_that_needs_a_person_waits_until_one_accepts(book: int) -> None:
    with SessionLocal() as session:
        _policy(session, NEEDS_A_PERSON)
        insight_id = _raise_it(session, book)["insight_id"]
        session.commit()

        outcomes = run_lifecycle_rules(session, run_id=book)
        session.commit()

        assert outcomes == {"waiting_for_a_person": 1}
        assert _changed(session) == {}
        assert session.get(AgentInsight, insight_id).state == "new"

        decide_insight(
            session, insight_id, decision="accept", reason="agreed", decided_by="reviewer-1"
        )
        session.commit()

        changed = _changed(session)
        assert set(changed) == set(MEETING_EVIDENCE)
        assert {row.changed_by for row in changed.values()} == {"reviewer-1"}
        assert session.get(AgentInsight, insight_id).state == "acted_on"
        assert session.get(AgentInsightLifecycle, insight_id).outcome == "applied"


def test_with_no_policy_the_finding_stands_as_a_note(book: int) -> None:
    with SessionLocal() as session:
        _policy(session, None)
        insight_id = _raise_it(session, book)["insight_id"]
        session.commit()

        outcomes = run_lifecycle_rules(session, run_id=book)
        session.commit()

        assert outcomes == {"no_policy": 1}
        assert _changed(session) == {}
        assert session.get(AgentInsight, insight_id).state == "new"
        link = session.get(AgentInsightLifecycle, insight_id)
        assert (link.outcome, link.policy_code) == ("no_policy", None)

        decide_insight(
            session, insight_id, decision="accept", reason="noted", decided_by="reviewer-1"
        )
        session.commit()

        assert _changed(session) == {}
        assert session.get(AgentInsight, insight_id).state == "accepted"
        with pytest.raises(InsightNotActionable):
            start_action_run(session, insight_id)


def test_a_big_change_waits_for_a_person_even_when_the_rule_acts_alone(
    book: int, monkeypatch
) -> None:
    monkeypatch.setenv("LIFECYCLE_AUTO_MAX_CLIENTS", "2")
    get_settings.cache_clear()
    try:
        with SessionLocal() as session:
            _policy(session, AUTOMATIC)
            _raise_it(session, book)
            session.commit()

            outcomes = run_lifecycle_rules(session, run_id=book)
            session.commit()

            assert outcomes == {"waiting_for_a_person": 1}
            assert _changed(session) == {}
    finally:
        get_settings.cache_clear()


def test_the_agent_has_no_way_to_change_a_label(book: int) -> None:
    with SessionLocal() as session:
        _policy(session, AUTOMATIC)
        written = _raise_it(session, book)
        session.commit()

        assert written["status"] == "written"
        assert _changed(session) == {}
        assert session.get(AgentInsightLifecycle, written["insight_id"]).outcome == "pending"

        refused = make_insight_tools(run_id=book)[WRITE_INSIGHT](
            session,
            kind="lifecycle_change",
            title="a change",
            group_name="a group",
            client_count=1,
            suggestion="change it",
            why_now="now",
            confidence="high",
            confidence_reason="sure",
        )
        assert refused["error"] == "use_lifecycle_tool"

    tree = ast.parse(Path(inspect.getfile(lifecycle_tools_module)).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
    for forbidden in (
        "lifecycle_state",
        "lifecycle_rules",
        "lifecycle_workflow",
        "lifecycle_policy",
    ):
        assert not any(forbidden in name for name in imported)

    for function in make_lifecycle_tools(run_id=book).values():
        closed_over = inspect.getclosurevars(function)
        reachable = {*closed_over.globals, *closed_over.nonlocals}
        assert not reachable & {"change_state", "settle_lifecycle_insight", "set_policy"}

    source_root = Path(lifecycle_tools_module.__file__).resolve().parents[1]
    writer = re.compile(r"(?<!class )ClientLifecycle\(|(insert|update|delete)\(ClientLifecycle")
    writers = {
        path.name
        for path in source_root.rglob("*.py")
        if writer.search(path.read_text(encoding="utf-8"))
    }
    assert writers == {"lifecycle_state.py"}

    exposed = {spec.name for spec in investigation_tool_specs()}
    assert {name for name in exposed if "lifecycle" in name} == {WRITE_LIFECYCLE_CHANGE}


def test_the_same_clients_are_not_raised_twice(book: int) -> None:
    with SessionLocal() as session:
        _policy(session, NEEDS_A_PERSON)
        first = _raise_it(session, book)
        session.commit()
        second = _raise_it(session, book)

        assert first["status"] == "written"
        assert second["error"] == "no_clients"


def test_a_policy_is_checked_and_every_change_to_it_is_audited(db: None) -> None:
    code = "test_dormant_to_active"
    arguments = {
        "policy_code": code,
        "from_state": "dormant",
        "to_state": "active",
        "evidence": [{"field": "deposit_trend", "op": "gt", "value": 0}],
        "mode": NEEDS_A_PERSON,
        "description": "a client who pays in again",
        "changed_by": "tester",
        "changed_reason": "test",
    }
    with SessionLocal() as session:
        try:
            with pytest.raises(PolicyRefused):
                set_policy(
                    session, **{**arguments, "evidence": [{"field": "client_id", "op": "eq"}]}
                )
            with pytest.raises(PolicyRefused):
                set_policy(session, **{**arguments, "evidence": []})
            with pytest.raises(PolicyRefused):
                set_policy(session, **{**arguments, "to_state": "dormant"})

            set_policy(session, **arguments)
            set_policy(session, **{**arguments, "mode": AUTOMATIC, "changed_reason": "safe now"})
            session.commit()

            trail = session.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "lifecycle_policy", AuditLog.entity_id == code)
                .order_by(AuditLog.log_id)
            ).all()
            assert [row.action for row in trail] == ["create", "update"]
            assert trail[1].detail["before"]["mode"] == NEEDS_A_PERSON
            assert trail[1].detail["after"]["mode"] == AUTOMATIC
        finally:
            session.rollback()
            _purge(session)


def test_accepting_on_the_screen_applies_the_change_and_starts_no_action(
    book: int, configured_reviewers, reviewer_1_headers
) -> None:
    client.headers.update(reviewer_1_headers)
    try:
        with SessionLocal() as session:
            _policy(session, NEEDS_A_PERSON)
            insight_id = _raise_it(session, book)["insight_id"]
            session.commit()

        before = client.get(f"{INSIGHTS}/{insight_id}").json()
        assert before["lifecycle"]["outcome"] == "pending"
        assert before["lifecycle"]["to_state"] == "dormant"

        decided = client.post(
            f"{INSIGHTS}/{insight_id}/decision", json={"decision": "accept", "reason": "agreed"}
        )

        assert decided.status_code == 200
        body = decided.json()
        assert body["state"] == "acted_on"
        assert body["lifecycle_outcome"] == "applied"
        assert body["action_run_id"] is None
    finally:
        client.headers.pop("Authorization", None)


async def test_a_run_notices_the_change_and_the_rules_step_makes_it(book: int, monkeypatch) -> None:
    monkeypatch.setattr(intelligence_module, "load_thresholds", lambda session, as_of: THRESHOLDS)
    with SessionLocal() as session:
        _policy(session, AUTOMATIC)
    arguments = {
        "from_state": "active",
        "to_state": "dormant",
        "title": "Clients who stopped paying in still labelled active",
        "why_now": "Their last deposit was over a year ago",
        "confidence": "high",
        "confidence_reason": "Every one of them shows the same gap",
        "conditions": EVERYONE,
    }
    scripts = {
        VERY_SMALL_AND_QUIET: [
            _calls((WRITE_LIFECYCLE_CHANGE, arguments)),
            _final_answer("One change written."),
        ]
    }

    with SessionLocal() as session:
        run = await run_intelligence_agent(
            session,
            trigger="manual",
            llm_client=ScriptedAsyncClient(scripts),
            as_of=AS_OF,
        )
        run_id = run.run_id
        state = run.state

    try:
        with SessionLocal() as session:
            insight = session.scalar(select(AgentInsight).where(AgentInsight.run_id == run_id))
            changed = _changed(session)
            trail = session.scalars(
                select(AuditLog).where(
                    AuditLog.entity_type == "client_lifecycle",
                    AuditLog.entity_id.like(f"%/{FUND_ID}"),
                )
            ).all()

        assert state == "completed"
        assert insight.kind == "lifecycle_change"
        assert insight.state == "acted_on"
        assert set(changed) == set(MEETING_EVIDENCE)
        assert {row.insight_id for row in changed.values()} == {insight.insight_id}
        assert {row.detail["policy_code"] for row in trail} == {"active_to_dormant"}
    finally:
        with SessionLocal() as session:
            _purge(session, run_id)
