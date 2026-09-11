"""The write tools: a finding can only come into being through a tool call,
every fact arrives with the filter behind it, a run cannot write more than
its cap, and none of it can reach a proposal, a campaign or the mailer.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.agents import insight_tools as insight_tools_module
from app.agents.insight_tools import (
    ADD_FACT,
    DISMISS_GROUP,
    INSIGHT_TOOL_NAMES,
    WRITE_INSIGHT,
    insight_tool_specs,
    insights_written,
    make_insight_tools,
)
from app.agents.tool_runtime import make_tool_executor
from app.config import get_settings
from app.db.models.agent_insight import AgentInsight, AgentInsightFact
from app.db.models.agent_run import AgentRun, AgentToolCall
from app.db.models.audit import AuditLog
from app.db.session import SessionLocal

GOOD_FILTER = [{"field": "risk_band", "op": "eq", "value": "high"}]


def _insight_input(**overrides):
    values = {
        "kind": "risk",
        "title": "A cluster of high risk clients has gone quiet",
        "group_name": "high risk, no deposit in six months",
        "group_definition": GOOD_FILTER,
        "client_count": 42,
        "money_total_kes": 3_400_000.0,
        "suggestion": "Call this group before the month ends",
        "avoid_saying": "Do not promise a return",
        "why_now": "The group has grown for three runs in a row",
        "confidence": "high",
        "confidence_reason": "The pattern held across every run this month",
    }
    values.update(overrides)
    return values


@pytest.fixture
def run(db: None):
    with SessionLocal() as session:
        row = AgentRun(trigger="manual")
        session.add(row)
        session.commit()
        run_id = row.run_id

    yield run_id

    with SessionLocal() as session:
        insight_ids = list(
            session.scalars(select(AgentInsight.insight_id).where(AgentInsight.run_id == run_id))
        )
        if insight_ids:
            session.execute(
                delete(AgentInsightFact).where(AgentInsightFact.insight_id.in_(insight_ids))
            )
        session.execute(delete(AgentInsight).where(AgentInsight.run_id == run_id))
        session.execute(delete(AgentToolCall).where(AgentToolCall.run_id == run_id))
        session.execute(delete(AuditLog).where(AuditLog.run_id == str(run_id)))
        session.execute(delete(AgentRun).where(AgentRun.run_id == run_id))
        session.commit()


def test_write_insight_writes_the_finding(run: int) -> None:
    tools = make_insight_tools(run_id=run)
    with SessionLocal() as session:
        output = tools[WRITE_INSIGHT](session, **_insight_input())
        session.commit()
        assert output["status"] == "written"
        row = session.get(AgentInsight, output["insight_id"])
        assert row.run_id == run
        assert row.kind == "risk"
        assert row.state == "new"
        assert row.client_count == 42
        assert row.group_definition == {"conditions": GOOD_FILTER}


def test_write_insight_refuses_an_unknown_kind(run: int) -> None:
    tools = make_insight_tools(run_id=run)
    with SessionLocal() as session:
        output = tools[WRITE_INSIGHT](session, **_insight_input(kind="hunch"))
        assert output["error"] == "unknown_kind"
        assert insights_written(session, run) == 0


def test_write_insight_refuses_an_empty_reason(run: int) -> None:
    tools = make_insight_tools(run_id=run)
    with SessionLocal() as session:
        output = tools[WRITE_INSIGHT](session, **_insight_input(confidence_reason="   "))
        assert output["error"] == "missing_text"
        assert "confidence_reason" in output["message"]


def test_write_insight_refuses_a_group_filter_off_the_allow_list(run: int) -> None:
    bad = [{"field": "client_code", "op": "eq", "value": "X"}]
    tools = make_insight_tools(run_id=run)
    with SessionLocal() as session:
        output = tools[WRITE_INSIGHT](session, **_insight_input(group_definition=bad))
        assert output["error"] == "bad_filter"
        assert "client_code" in output["message"]


def test_write_insight_records_its_audit_row(run: int) -> None:
    tools = make_insight_tools(run_id=run)
    with SessionLocal() as session:
        output = tools[WRITE_INSIGHT](session, **_insight_input())
        session.commit()
        row = session.scalar(
            select(AuditLog).where(
                AuditLog.entity_type == "agent_insight",
                AuditLog.entity_id == str(output["insight_id"]),
            )
        )
        assert row is not None
        assert row.action == "write"


def test_the_cap_stops_a_run_flooding_the_screen(run: int, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_INSIGHT_WRITE_CAP", "2")
    get_settings.cache_clear()
    try:
        tools = make_insight_tools(run_id=run)
        with SessionLocal() as session:
            for _ in range(2):
                assert tools[WRITE_INSIGHT](session, **_insight_input())["status"] == "written"
            refused = tools[WRITE_INSIGHT](session, **_insight_input())
            session.commit()
            assert refused["error"] == "write_cap_reached"
            assert insights_written(session, run) == 2
    finally:
        get_settings.cache_clear()


def test_add_fact_hangs_a_number_on_a_finding(run: int) -> None:
    tools = make_insight_tools(run_id=run)
    with SessionLocal() as session:
        written = tools[WRITE_INSIGHT](session, **_insight_input())
        output = tools[ADD_FACT](
            session,
            insight_id=written["insight_id"],
            fact_text="clients in this group",
            fact_value="42",
            source_table="client_risk_features",
            conditions=GOOD_FILTER,
        )
        session.commit()
        assert output["status"] == "recorded"
        fact = session.get(AgentInsightFact, output["fact_id"])
        assert fact.source_filter == {"conditions": GOOD_FILTER}
        assert fact.source_table == "client_risk_features"


def test_add_fact_refuses_a_fact_with_no_filter(run: int) -> None:
    tools = make_insight_tools(run_id=run)
    with SessionLocal() as session:
        written = tools[WRITE_INSIGHT](session, **_insight_input())
        output = tools[ADD_FACT](
            session,
            insight_id=written["insight_id"],
            fact_text="clients in this group",
            fact_value="42",
            source_table="client_risk_features",
        )
        session.commit()
        assert output["error"] == "missing_filter"
        assert (
            session.scalar(
                select(AgentInsightFact).where(AgentInsightFact.insight_id == written["insight_id"])
            )
            is None
        )


def test_add_fact_refuses_an_insight_this_run_did_not_write(run: int) -> None:
    tools = make_insight_tools(run_id=run)
    with SessionLocal() as session:
        output = tools[ADD_FACT](
            session,
            insight_id=-1,
            fact_text="clients",
            fact_value="42",
            source_table="client_risk_features",
            conditions=GOOD_FILTER,
        )
        assert output["error"] == "unknown_insight"


def test_add_fact_refuses_a_table_it_did_not_count_from(run: int) -> None:
    tools = make_insight_tools(run_id=run)
    with SessionLocal() as session:
        written = tools[WRITE_INSIGHT](session, **_insight_input())
        output = tools[ADD_FACT](
            session,
            insight_id=written["insight_id"],
            fact_text="clients",
            fact_value="42",
            source_table="pii_vault",
            conditions=GOOD_FILTER,
        )
        assert output["error"] == "unknown_source_table"


def test_add_fact_records_its_audit_row(run: int) -> None:
    tools = make_insight_tools(run_id=run)
    with SessionLocal() as session:
        written = tools[WRITE_INSIGHT](session, **_insight_input())
        output = tools[ADD_FACT](
            session,
            insight_id=written["insight_id"],
            fact_text="clients",
            fact_value="42",
            source_table="client_risk_features",
            conditions=GOOD_FILTER,
        )
        session.commit()
        row = session.scalar(
            select(AuditLog).where(
                AuditLog.entity_type == "agent_insight_fact",
                AuditLog.entity_id == str(output["fact_id"]),
            )
        )
        assert row is not None


def test_dismiss_group_is_a_record_not_a_silence(run: int) -> None:
    seen: list[dict] = []
    tools = make_insight_tools(run_id=run, dismissals=seen)
    with SessionLocal() as session:
        output = tools[DISMISS_GROUP](
            session,
            group_name="low risk, small balances",
            reason="Nothing has moved here since the last run",
            conditions=GOOD_FILTER,
        )
        session.commit()
        assert output["status"] == "noted"
        row = session.scalar(
            select(AuditLog).where(AuditLog.action == "dismiss_group", AuditLog.run_id == str(run))
        )
        assert row is not None
        assert row.detail["reason"] == "Nothing has moved here since the last run"
    assert seen[0]["group_name"] == "low risk, small balances"


def test_dismiss_group_refuses_a_dismissal_with_no_reason(run: int) -> None:
    tools = make_insight_tools(run_id=run)
    with SessionLocal() as session:
        output = tools[DISMISS_GROUP](session, group_name="a group", reason="  ")
        assert output["error"] == "missing_reason"


def test_a_write_tool_call_is_recorded_like_any_other(run: int) -> None:
    tools = make_insight_tools(run_id=run)
    with SessionLocal() as session:
        call_tool = make_tool_executor(session, run, extra_tools=tools)
        output = call_tool(WRITE_INSIGHT, _insight_input())
        session.commit()
        row = session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run))
        assert row.tool_name == WRITE_INSIGHT
        assert row.tool_output["insight_id"] == output["insight_id"]


def test_the_specs_cover_every_write_tool() -> None:
    names = {spec.name for spec in insight_tool_specs()}
    assert names == set(INSIGHT_TOOL_NAMES)


def test_a_fact_spec_requires_its_filter() -> None:
    spec = next(spec for spec in insight_tool_specs() if spec.name == ADD_FACT)
    assert "conditions" in spec.input_schema["required"]


def test_no_write_tool_can_propose_send_or_start_a_campaign() -> None:
    source = Path(inspect.getfile(insight_tools_module)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)

    forbidden = ("proposal", "campaign", "delivery", "mail", "send")
    for name in imported:
        lowered = name.lower()
        for word in forbidden:
            assert word not in lowered, f"a write tool must not reach {name}"

    tools = make_insight_tools(run_id=1)
    assert set(tools) == set(INSIGHT_TOOL_NAMES)
    for function in tools.values():
        closed_over = inspect.getclosurevars(function)
        reachable = {*closed_over.globals, *closed_over.nonlocals}
        for name in reachable:
            lowered = name.lower()
            for word in forbidden:
                assert word not in lowered, f"a write tool must not reach {name}"
