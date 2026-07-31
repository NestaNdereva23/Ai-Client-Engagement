"""The daily scheduler: which enrollments are due, and how they advance.

Covers select_due_enrollments picking up a fresh enrollment immediately and
skipping one that is not due yet or has left the active states, and
advance_enrollment moving current_step forward with next_due_at measured
from the step offsets, finishing the enrollment on the last step, and
being a no-op if called again for a step already passed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.campaigns.scheduler import advance_enrollment, select_due_enrollments
from app.db.models.audit import AuditLog
from app.db.models.campaigns import CampaignStep, Enrollment
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign
from app.db.session import SessionLocal


@pytest.fixture
def campaign_with_steps(db: None):
    """A weekly-for-a-month campaign: four steps at day 0, 7, 14, 21."""
    with SessionLocal() as session:
        campaign = Campaign(name="test scheduler campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id

        for step_no, offset_days in enumerate((0, 7, 14, 21), start=1):
            session.add(
                CampaignStep(
                    campaign_id=campaign_id,
                    step_no=step_no,
                    offset_days=offset_days,
                    message_angle="winback_habit",
                )
            )
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "enrollment", AuditLog.action == "advance"
            )
        )
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


@pytest.fixture
def client_row(db: None):
    fund_id = 990
    client_id = 99001
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.commit()

    yield client_id

    with SessionLocal() as session:
        session.execute(delete(Enrollment).where(Enrollment.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def _make_enrollment(session, *, campaign_id: int, client_id: int, **overrides) -> Enrollment:
    row = Enrollment(campaign_id=campaign_id, client_id=client_id, **overrides)
    session.add(row)
    session.commit()
    return row


def test_a_fresh_enrollment_is_due_immediately(campaign_with_steps: int, client_row: int) -> None:
    with SessionLocal() as session:
        _make_enrollment(session, campaign_id=campaign_with_steps, client_id=client_row)

    with SessionLocal() as session:
        due = select_due_enrollments(session, campaign_id=campaign_with_steps)
    assert [row.client_id for row in due] == [client_row]


def test_an_enrollment_not_yet_due_is_excluded(campaign_with_steps: int, client_row: int) -> None:
    with SessionLocal() as session:
        _make_enrollment(
            session,
            campaign_id=campaign_with_steps,
            client_id=client_row,
            current_step=1,
            next_due_at=datetime.now(UTC) + timedelta(days=3),
        )

    with SessionLocal() as session:
        due = select_due_enrollments(session, campaign_id=campaign_with_steps)
    assert due == []


@pytest.mark.parametrize(
    "status", ["completed", "excluded", "stopped_reply", "stopped_optout", "stopped_bounce"]
)
def test_a_terminal_status_is_excluded_even_if_next_due_at_has_passed(
    campaign_with_steps: int, client_row: int, status: str
) -> None:
    with SessionLocal() as session:
        _make_enrollment(
            session,
            campaign_id=campaign_with_steps,
            client_id=client_row,
            current_step=4,
            status=status,
            next_due_at=datetime.now(UTC) - timedelta(days=1),
        )

    with SessionLocal() as session:
        due = select_due_enrollments(session, campaign_id=campaign_with_steps)
    assert due == []


def test_select_due_enrollments_respects_the_batch_limit(
    campaign_with_steps: int, client_row: int
) -> None:
    with SessionLocal() as session:
        _make_enrollment(session, campaign_id=campaign_with_steps, client_id=client_row)

    with SessionLocal() as session:
        due = select_due_enrollments(session, campaign_id=campaign_with_steps, limit=0)
    assert due == []


def test_advance_enrollment_schedules_the_next_step_from_the_offset_gap(
    campaign_with_steps: int, client_row: int
) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=client_row
        )
        sent_at = datetime.now(UTC)
        advance_enrollment(session, enrollment, step_no=1, sent_at=sent_at)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment_id)
        assert row.current_step == 1
        assert row.status == "in_progress"
        assert abs((row.next_due_at - (sent_at + timedelta(days=7))).total_seconds()) < 2


def test_advance_enrollment_completes_the_enrollment_on_the_last_step(
    campaign_with_steps: int, client_row: int
) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=client_row, current_step=3
        )
        advance_enrollment(session, enrollment, step_no=4, sent_at=datetime.now(UTC))
        session.commit()
        enrollment_id = enrollment.enrollment_id

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment_id)
        assert row.current_step == 4
        assert row.status == "completed"
        assert row.next_due_at is None


def test_advance_enrollment_is_a_no_op_for_a_step_already_passed(
    campaign_with_steps: int, client_row: int
) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=client_row
        )
        advance_enrollment(session, enrollment, step_no=1, sent_at=datetime.now(UTC))
        session.commit()
        first_due_at = enrollment.next_due_at
        enrollment_id = enrollment.enrollment_id

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment_id)
        advance_enrollment(session, row, step_no=1, sent_at=datetime.now(UTC))
        session.commit()

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment_id)
        assert row.current_step == 1
        assert row.next_due_at == first_due_at


def test_advance_enrollment_writes_an_audit_row(campaign_with_steps: int, client_row: int) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=client_row
        )
        advance_enrollment(session, enrollment, step_no=1, sent_at=datetime.now(UTC))
        session.commit()
        enrollment_id = enrollment.enrollment_id

    with SessionLocal() as session:
        rows = (
            session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "enrollment",
                    AuditLog.entity_id == str(enrollment_id),
                    AuditLog.action == "advance",
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].detail["step_no"] == 1
