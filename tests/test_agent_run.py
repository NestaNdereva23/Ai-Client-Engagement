"""The agent run tables: a run can be created by hand, and its tool calls
read back in the order they happened.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.db.models.agent_run import AgentRun, AgentToolCall
from app.db.session import SessionLocal


@pytest.fixture
def run(db: None):
    with SessionLocal() as session:
        row = AgentRun(trigger="nightly")
        session.add(row)
        session.commit()
        run_id = row.run_id

    yield run_id

    with SessionLocal() as session:
        session.execute(delete(AgentToolCall).where(AgentToolCall.run_id == run_id))
        session.execute(delete(AgentRun).where(AgentRun.run_id == run_id))
        session.commit()


def test_a_run_can_be_created_by_hand(run: int) -> None:
    with SessionLocal() as session:
        row = session.get(AgentRun, run)
    assert row.state == "running"
    assert row.finished_at is None
    assert row.plan_text is None
    assert row.cost_kes is None


def test_an_unknown_trigger_is_refused(db: None) -> None:
    with SessionLocal() as session:
        session.add(AgentRun(trigger="whenever_it_feels_like_it"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_an_unknown_state_is_refused(run: int) -> None:
    with SessionLocal() as session:
        row = session.get(AgentRun, run)
        row.state = "thinking"
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_a_run_can_finish_with_a_plan_a_summary_and_a_cost(run: int) -> None:
    with SessionLocal() as session:
        row = session.get(AgentRun, run)
        row.state = "completed"
        row.plan_text = "fee warning group matters tonight, the rest can wait"
        row.summary = "one proposal written, none sent yet"
        row.cost_kes = 4.5
        session.commit()

    with SessionLocal() as session:
        row = session.get(AgentRun, run)
    assert row.state == "completed"
    assert row.plan_text == "fee warning group matters tonight, the rest can wait"
    assert row.summary == "one proposal written, none sent yet"
    assert row.cost_kes == 4.5


def test_tool_calls_read_back_in_the_order_they_happened(run: int) -> None:
    with SessionLocal() as session:
        session.add_all(
            [
                AgentToolCall(
                    run_id=run,
                    ordinal=1,
                    tool_name="list_groups",
                    tool_input={},
                    tool_output={"groups": ["fees_will_empty"]},
                ),
                AgentToolCall(
                    run_id=run,
                    ordinal=3,
                    tool_name="check_allowance",
                    tool_input={"action_code": "fee_warning"},
                    tool_output={"remaining": 40},
                ),
                AgentToolCall(
                    run_id=run,
                    ordinal=2,
                    tool_name="describe_group",
                    tool_input={"group_name": "fees_will_empty"},
                    tool_output={"client_count": 12},
                ),
            ]
        )
        session.commit()

    with SessionLocal() as session:
        rows = session.scalars(
            select(AgentToolCall).where(AgentToolCall.run_id == run).order_by(AgentToolCall.ordinal)
        ).all()
    assert [row.tool_name for row in rows] == ["list_groups", "describe_group", "check_allowance"]
    assert rows[1].tool_input == {"group_name": "fees_will_empty"}
    assert rows[2].tool_output == {"remaining": 40}


def test_the_same_run_cannot_reuse_an_ordinal(run: int) -> None:
    with SessionLocal() as session:
        session.add(
            AgentToolCall(
                run_id=run, ordinal=1, tool_name="list_groups", tool_input={}, tool_output={}
            )
        )
        session.commit()

    with SessionLocal() as session:
        session.add(
            AgentToolCall(
                run_id=run, ordinal=1, tool_name="describe_group", tool_input={}, tool_output={}
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
