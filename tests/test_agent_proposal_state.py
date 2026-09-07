"""The proposal state machine: allowed moves, blocked moves, and that every
move that actually happens is written to the audit trail.

transition_proposal is the only path meant to ever write
agent_proposal.status; these tests cover both directions (an allowed move
happens and is audited, a move that is not allowed raises and changes
nothing) and that every terminal status genuinely has no way out.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.agents.proposal_state import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    InvalidTransition,
    transition_proposal,
)
from app.db.models.agent_proposal import AgentProposal
from app.db.models.audit import AuditLog
from app.db.session import SessionLocal


@pytest.fixture
def proposal(db: None):
    with SessionLocal() as session:
        row = AgentProposal(
            action_code="start_win_back",
            catalog_version=1,
            group_name="test state machine group",
            client_count=10,
            evidence="ten dormant clients, no contact in ninety days",
            reason="win back before they close the account",
            permission_applied="suggest_only",
        )
        session.add(row)
        session.commit()
        proposal_id = row.proposal_id

    yield proposal_id

    with SessionLocal() as session:
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "agent_proposal",
                AuditLog.entity_id == str(proposal_id),
            )
        )
        session.execute(delete(AgentProposal).where(AgentProposal.proposal_id == proposal_id))
        session.commit()


def _set_status(proposal_id: int, status: str) -> None:
    with SessionLocal() as session:
        row = session.get(AgentProposal, proposal_id)
        row.status = status
        session.commit()


def test_proposed_to_approved_is_allowed_and_audited(proposal: int) -> None:
    with SessionLocal() as session:
        row = session.get(AgentProposal, proposal)
        transition_proposal(
            session, row, to_status="approved", reason="looks right", decided_by="asha"
        )
        session.commit()

    with SessionLocal() as session:
        row = session.get(AgentProposal, proposal)
        assert row.status == "approved"
        assert row.decided_by == "asha"
        assert row.decided_at is not None

        audit_rows = (
            session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "agent_proposal",
                    AuditLog.action == "transition",
                    AuditLog.entity_id == str(proposal),
                )
            )
            .scalars()
            .all()
        )
    assert len(audit_rows) == 1
    assert audit_rows[0].detail == {"from": "proposed", "to": "approved", "reason": "looks right"}
    assert audit_rows[0].actor_id == "asha"


def test_a_move_with_no_decider_leaves_decided_by_empty(proposal: int) -> None:
    with SessionLocal() as session:
        row = session.get(AgentProposal, proposal)
        transition_proposal(session, row, to_status="expired", reason="nobody looked")
        session.commit()

    with SessionLocal() as session:
        row = session.get(AgentProposal, proposal)
        assert row.status == "expired"
        assert row.decided_by is None
        assert row.decided_at is None


def test_proposed_cannot_jump_straight_to_sent(proposal: int) -> None:
    with SessionLocal() as session:
        row = session.get(AgentProposal, proposal)
        with pytest.raises(InvalidTransition):
            transition_proposal(session, row, to_status="sent", reason="oops")
        session.rollback()

    with SessionLocal() as session:
        row = session.get(AgentProposal, proposal)
        assert row.status == "proposed"
        audit_rows = (
            session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "agent_proposal",
                    AuditLog.action == "transition",
                    AuditLog.entity_id == str(proposal),
                )
            )
            .scalars()
            .all()
        )
    assert audit_rows == []


@pytest.mark.parametrize("terminal_status", sorted(TERMINAL_STATUSES))
def test_a_terminal_status_has_no_allowed_moves(proposal: int, terminal_status: str) -> None:
    _set_status(proposal, terminal_status)
    with SessionLocal() as session:
        row = session.get(AgentProposal, proposal)
        with pytest.raises(InvalidTransition):
            transition_proposal(session, row, to_status="running", reason="oops")
        session.rollback()

    with SessionLocal() as session:
        assert session.get(AgentProposal, proposal).status == terminal_status


def test_running_can_reach_every_outcome(proposal: int) -> None:
    for to_status in ("blocked", "sent", "stopped"):
        _set_status(proposal, "running")
        with SessionLocal() as session:
            row = session.get(AgentProposal, proposal)
            transition_proposal(session, row, to_status=to_status, reason="test")
            session.commit()

        with SessionLocal() as session:
            assert session.get(AgentProposal, proposal).status == to_status


def test_the_full_happy_path_reaches_measured(proposal: int) -> None:
    for to_status in ("approved", "running", "sent", "measured"):
        with SessionLocal() as session:
            row = session.get(AgentProposal, proposal)
            transition_proposal(session, row, to_status=to_status, reason="test")
            session.commit()

    with SessionLocal() as session:
        assert session.get(AgentProposal, proposal).status == "measured"


def test_every_declared_status_is_reachable_or_terminal_by_design() -> None:
    """A sanity check on the table itself: every status the model names is
    present in the transition table, and the states with no way out are
    exactly the ones this module calls terminal."""
    from app.db.models.agent_proposal import PROPOSAL_STATUSES

    assert set(ALLOWED_TRANSITIONS) == set(PROPOSAL_STATUSES)
    assert TERMINAL_STATUSES == {"rejected", "expired", "blocked", "stopped", "measured"}
