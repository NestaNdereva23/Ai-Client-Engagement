"""The insight state machine: allowed moves, blocked moves, and that every
move that actually happens is written to the audit trail.

transition_insight is the only path meant to ever write agent_insight.state;
these tests cover both directions (an allowed move happens and is audited, a
move that is not allowed raises and changes nothing), that every terminal
state genuinely has no way out, and that dismissing keeps a reason.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.agents.insight_state import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    InvalidTransition,
    transition_insight,
)
from app.db.models.agent_insight import AgentInsight
from app.db.models.audit import AuditLog
from app.db.session import SessionLocal


@pytest.fixture
def insight(db: None):
    with SessionLocal() as session:
        row = AgentInsight(
            kind="opportunity",
            title="a group of clients is ready for a second fund",
            group_name="test insight state group",
            client_count=8,
            confidence="medium",
            confidence_reason="the pattern holds for most of the group but not all of it",
            suggestion="offer them the second fund",
            why_now="they all topped up in the last month",
        )
        session.add(row)
        session.commit()
        insight_id = row.insight_id

    yield insight_id

    with SessionLocal() as session:
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "agent_insight",
                AuditLog.entity_id == str(insight_id),
            )
        )
        session.execute(delete(AgentInsight).where(AgentInsight.insight_id == insight_id))
        session.commit()


def _set_state(insight_id: int, state: str) -> None:
    with SessionLocal() as session:
        row = session.get(AgentInsight, insight_id)
        row.state = state
        if state == "dismissed":
            row.dismissed_reason = "set up for the test"
        session.commit()


def _audit_rows(session, insight_id: int):
    return (
        session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "agent_insight",
                AuditLog.action == "transition",
                AuditLog.entity_id == str(insight_id),
            )
        )
        .scalars()
        .all()
    )


def test_new_to_accepted_is_allowed_and_audited(insight: int) -> None:
    with SessionLocal() as session:
        row = session.get(AgentInsight, insight)
        transition_insight(
            session, row, to_state="accepted", reason="worth acting on", decided_by="asha"
        )
        session.commit()

    with SessionLocal() as session:
        row = session.get(AgentInsight, insight)
        assert row.state == "accepted"
        assert row.decided_by == "asha"
        assert row.decided_at is not None
        rows = _audit_rows(session, insight)
    assert len(rows) == 1
    assert rows[0].detail == {"from": "new", "to": "accepted", "reason": "worth acting on"}
    assert rows[0].actor_id == "asha"


def test_a_move_with_no_decider_leaves_decided_by_empty(insight: int) -> None:
    with SessionLocal() as session:
        row = session.get(AgentInsight, insight)
        transition_insight(session, row, to_state="expired", reason="nobody looked")
        session.commit()

    with SessionLocal() as session:
        row = session.get(AgentInsight, insight)
        assert row.state == "expired"
        assert row.decided_by is None
        assert row.decided_at is None


def test_dismissing_keeps_the_reason_on_the_row(insight: int) -> None:
    with SessionLocal() as session:
        row = session.get(AgentInsight, insight)
        transition_insight(
            session, row, to_state="dismissed", reason="we already called them", decided_by="asha"
        )
        session.commit()

    with SessionLocal() as session:
        row = session.get(AgentInsight, insight)
    assert row.state == "dismissed"
    assert row.dismissed_reason == "we already called them"


def test_dismissing_without_a_reason_is_refused(insight: int) -> None:
    with SessionLocal() as session:
        row = session.get(AgentInsight, insight)
        with pytest.raises(InvalidTransition):
            transition_insight(session, row, to_state="dismissed", reason="   ")
        session.rollback()

    with SessionLocal() as session:
        row = session.get(AgentInsight, insight)
        assert row.state == "new"
        assert row.dismissed_reason is None


def test_new_cannot_jump_straight_to_acted_on(insight: int) -> None:
    with SessionLocal() as session:
        row = session.get(AgentInsight, insight)
        with pytest.raises(InvalidTransition):
            transition_insight(session, row, to_state="acted_on", reason="oops")
        session.rollback()

    with SessionLocal() as session:
        row = session.get(AgentInsight, insight)
        assert row.state == "new"
        assert _audit_rows(session, insight) == []


@pytest.mark.parametrize("terminal_state", sorted(TERMINAL_STATES))
def test_a_terminal_state_has_no_allowed_moves(insight: int, terminal_state: str) -> None:
    _set_state(insight, terminal_state)
    with SessionLocal() as session:
        row = session.get(AgentInsight, insight)
        with pytest.raises(InvalidTransition):
            transition_insight(session, row, to_state="accepted", reason="oops")
        session.rollback()

    with SessionLocal() as session:
        assert session.get(AgentInsight, insight).state == terminal_state


def test_accepted_can_reach_every_outcome(insight: int) -> None:
    for to_state in ("acted_on", "dismissed", "expired"):
        _set_state(insight, "accepted")
        with SessionLocal() as session:
            row = session.get(AgentInsight, insight)
            transition_insight(session, row, to_state=to_state, reason="test")
            session.commit()

        with SessionLocal() as session:
            assert session.get(AgentInsight, insight).state == to_state


def test_the_full_happy_path_reaches_acted_on(insight: int) -> None:
    for to_state in ("accepted", "acted_on"):
        with SessionLocal() as session:
            row = session.get(AgentInsight, insight)
            transition_insight(session, row, to_state=to_state, reason="test")
            session.commit()

    with SessionLocal() as session:
        assert session.get(AgentInsight, insight).state == "acted_on"
        assert len(_audit_rows(session, insight)) == 2


def test_every_declared_state_is_in_the_table_and_terminals_match() -> None:
    """A sanity check on the table itself: every state the model names is
    present in the transition table, and the states with no way out are
    exactly the ones this module calls terminal."""
    from app.db.models.agent_insight import INSIGHT_STATES

    assert set(ALLOWED_TRANSITIONS) == set(INSIGHT_STATES)
    assert TERMINAL_STATES == {"acted_on", "dismissed", "expired"}
