"""The tool dispatcher: turning a tool name into a call, scanning what it
returns, and recording every attempt to agent_tool_call.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.agents import tool_runtime as runtime_module
from app.agents.tool_runtime import (
    UnknownTool,
    make_tool_executor,
    next_ordinal,
    record_tool_call,
    run_tool,
)
from app.db.models.agent_run import AgentRun, AgentToolCall
from app.db.session import SessionLocal
from app.privacy.scanners import OutboundLeak


@pytest.fixture
def run(db: None):
    with SessionLocal() as session:
        row = AgentRun(trigger="manual")
        session.add(row)
        session.commit()
        run_id = row.run_id

    yield run_id

    with SessionLocal() as session:
        session.execute(delete(AgentToolCall).where(AgentToolCall.run_id == run_id))
        session.execute(delete(AgentRun).where(AgentRun.run_id == run_id))
        session.commit()


def test_run_tool_raises_for_a_name_outside_the_registry(db: None) -> None:
    with SessionLocal() as session:
        with pytest.raises(UnknownTool):
            run_tool(session, "not_a_real_tool", {})


def test_run_tool_calls_the_matching_function(db: None, monkeypatch) -> None:
    calls: list[tuple] = []

    def fake_list_groups(session, **kwargs):
        calls.append((session, kwargs))
        return {"groups": []}

    monkeypatch.setattr(runtime_module, "TOOL_FUNCTIONS", {"list_groups": fake_list_groups})
    with SessionLocal() as session:
        output = run_tool(session, "list_groups", {"as_of": "2026-09-07"})
    assert output == {"groups": []}
    assert calls[0][1] == {"as_of": "2026-09-07"}


def test_next_ordinal_starts_at_one(run: int) -> None:
    with SessionLocal() as session:
        assert next_ordinal(session, run) == 1


def test_next_ordinal_follows_the_highest_recorded_call(run: int) -> None:
    with SessionLocal() as session:
        record_tool_call(
            session,
            run_id=run,
            ordinal=1,
            tool_name="list_groups",
            tool_input={},
            tool_output={"groups": []},
        )
        session.commit()
        assert next_ordinal(session, run) == 2


def test_record_tool_call_writes_the_full_row(run: int) -> None:
    with SessionLocal() as session:
        record_tool_call(
            session,
            run_id=run,
            ordinal=1,
            tool_name="describe_group",
            tool_input={"group_name": "fees_will_empty"},
            tool_output={"client_count": 3},
        )
        session.commit()

    with SessionLocal() as session:
        row = session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run))
    assert row.tool_name == "describe_group"
    assert row.tool_input == {"group_name": "fees_will_empty"}
    assert row.tool_output == {"client_count": 3}


def test_call_tool_records_a_successful_call_and_returns_its_output(run: int, monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_module, "TOOL_FUNCTIONS", {"list_groups": lambda session, **kw: {"groups": []}}
    )
    with SessionLocal() as session:
        call_tool = make_tool_executor(session, run)
        output = call_tool("list_groups", {})
        session.commit()

    assert output == {"groups": []}
    with SessionLocal() as session:
        row = session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run))
    assert row.tool_name == "list_groups"
    assert row.tool_output == {"groups": []}
    assert row.ordinal == 1


def test_call_tool_records_and_returns_a_refusal_for_an_unknown_tool(run: int) -> None:
    with SessionLocal() as session:
        call_tool = make_tool_executor(session, run)
        output = call_tool("not_a_real_tool", {})
        session.commit()

    assert output["error"] == "unknown_tool"
    with SessionLocal() as session:
        row = session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run))
    assert row.tool_output["error"] == "unknown_tool"


def test_call_tool_records_and_returns_a_refusal_for_a_bad_argument(run: int, monkeypatch) -> None:
    def strict_tool(session, *, group_name):
        return {"group_name": group_name}

    monkeypatch.setattr(runtime_module, "TOOL_FUNCTIONS", {"describe_group": strict_tool})
    with SessionLocal() as session:
        call_tool = make_tool_executor(session, run)
        output = call_tool("describe_group", {"not_a_real_argument": "x"})
        session.commit()

    assert output["error"] == "invalid_input"
    with SessionLocal() as session:
        row = session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run))
    assert row.tool_output["error"] == "invalid_input"


def test_call_tool_withholds_and_raises_when_the_output_leaks(run: int, monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_module,
        "TOOL_FUNCTIONS",
        {"leaky_tool": lambda session, **kw: {"contact": "jane@example.com"}},
    )
    with SessionLocal() as session:
        call_tool = make_tool_executor(session, run)
        with pytest.raises(OutboundLeak):
            call_tool("leaky_tool", {})
        session.commit()

    with SessionLocal() as session:
        row = session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run))
    assert row.tool_output == {
        "error": "output_blocked",
        "message": "this tool's answer was withheld",
    }
    assert "jane@example.com" not in str(row.tool_output)


def test_call_tool_gives_each_call_in_a_run_its_own_ordinal(run: int, monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_module, "TOOL_FUNCTIONS", {"list_groups": lambda session, **kw: {"groups": []}}
    )
    with SessionLocal() as session:
        call_tool = make_tool_executor(session, run)
        call_tool("list_groups", {})
        call_tool("list_groups", {"as_of": "2026-09-07"})
        session.commit()

    with SessionLocal() as session:
        rows = session.scalars(
            select(AgentToolCall).where(AgentToolCall.run_id == run).order_by(AgentToolCall.ordinal)
        ).all()
    assert [row.ordinal for row in rows] == [1, 2]
