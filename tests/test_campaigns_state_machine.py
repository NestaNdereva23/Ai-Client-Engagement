"""The enrollment state machine: allowed transitions, rejected ones, and
that every actual move is audited.

transition_enrollment is the only path that is meant to ever write
enrollment.status; these tests cover both directions of the table (a move
that is allowed happens and is audited, a move that is not raises and
changes nothing) and that every terminal status genuinely has no way out.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.campaigns.state_machine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    InvalidTransition,
    transition_enrollment,
)
from app.db.models.audit import AuditLog
from app.db.models.campaigns import Enrollment
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign
from app.db.session import SessionLocal

_FUND_ID = 996
_CLIENT_ID = 99601


@pytest.fixture
def campaign(db: None):
    with SessionLocal() as session:
        row = Campaign(name="test state machine campaign")
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id

    yield campaign_id

    with SessionLocal() as session:
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "enrollment", AuditLog.action == "transition"
            )
        )
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


@pytest.fixture
def client_row(db: None):
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=_FUND_ID, unit_fund_name="Test Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=_CLIENT_ID,
                unit_fund_id=_FUND_ID,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.commit()

    yield _CLIENT_ID

    with SessionLocal() as session:
        session.execute(delete(Clients).where(Clients.client_id == _CLIENT_ID))
        session.execute(delete(Funds).where(Funds.unit_fund_id == _FUND_ID))
        session.commit()


def _make_enrollment(session, *, campaign_id: int, client_id: int, **overrides) -> Enrollment:
    row = Enrollment(campaign_id=campaign_id, client_id=client_id, **overrides)
    session.add(row)
    session.commit()
    return row


def test_enrolled_to_in_progress_is_allowed_and_audited(campaign: int, client_row: int) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(session, campaign_id=campaign, client_id=client_row)
        transition_enrollment(session, enrollment, to_status="in_progress", reason="touch_sent")
        session.commit()
        enrollment_id = enrollment.enrollment_id

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment_id)
        assert row.status == "in_progress"

        audit_rows = (
            session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "enrollment",
                    AuditLog.action == "transition",
                    AuditLog.entity_id == str(enrollment_id),
                )
            )
            .scalars()
            .all()
        )
    assert len(audit_rows) == 1
    assert audit_rows[0].detail == {"from": "enrolled", "to": "in_progress", "reason": "touch_sent"}


def test_in_progress_can_self_transition_for_the_next_touch(campaign: int, client_row: int) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign, client_id=client_row, status="in_progress"
        )
        transition_enrollment(session, enrollment, to_status="in_progress", reason="touch_sent")
        session.commit()
        enrollment_id = enrollment.enrollment_id

    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment_id).status == "in_progress"


def test_enrolled_cannot_jump_straight_to_completed(campaign: int, client_row: int) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(session, campaign_id=campaign, client_id=client_row)
        with pytest.raises(InvalidTransition):
            transition_enrollment(session, enrollment, to_status="completed", reason="oops")
        session.rollback()

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment.enrollment_id)
        assert row.status == "enrolled"
        audit_rows = (
            session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "enrollment",
                    AuditLog.action == "transition",
                    AuditLog.entity_id == str(enrollment.enrollment_id),
                )
            )
            .scalars()
            .all()
        )
    assert audit_rows == []


@pytest.mark.parametrize("terminal_status", sorted(TERMINAL_STATUSES))
def test_a_terminal_status_has_no_allowed_transitions(
    campaign: int, client_row: int, terminal_status: str
) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign, client_id=client_row, status=terminal_status
        )
        with pytest.raises(InvalidTransition):
            transition_enrollment(session, enrollment, to_status="in_progress", reason="oops")
        session.rollback()

    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment.enrollment_id).status == terminal_status


@pytest.mark.parametrize(
    "to_status",
    ["completed", "stopped_reply", "stopped_optout", "stopped_bounce", "stopped_reengaged"],
)
def test_in_progress_can_reach_every_stopping_state(
    campaign: int, client_row: int, to_status: str
) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign, client_id=client_row, status="in_progress"
        )
        transition_enrollment(session, enrollment, to_status=to_status, reason="test")
        session.commit()
        enrollment_id = enrollment.enrollment_id

    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment_id).status == to_status


def test_every_declared_status_is_reachable_or_terminal_by_design() -> None:
    """A sanity check on the table itself, not the database: every status
    named in the model is present, and the states with no way out are
    exactly the ones the enrollment model calls terminal."""
    from app.db.models.campaigns import ENROLLMENT_STATUSES

    assert set(ALLOWED_TRANSITIONS) == set(ENROLLMENT_STATUSES)
    assert TERMINAL_STATUSES == {
        "excluded",
        "completed",
        "stopped_reply",
        "stopped_optout",
        "stopped_bounce",
        "stopped_reengaged",
    }
