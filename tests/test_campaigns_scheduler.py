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

from app.campaigns.scheduler import advance_enrollment, count_stale_contacts, select_due_enrollments
from app.db.models.audit import AuditLog
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog
from app.db.models.models import ClientFeatures, Clients, Funds
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


@pytest.fixture
def one_step_campaign(db: None):
    """A single-touch campaign: step 1 is also the last step."""
    with SessionLocal() as session:
        campaign = Campaign(name="test one-step campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id
        session.add(
            CampaignStep(
                campaign_id=campaign_id, step_no=1, offset_days=0, message_angle="winback_habit"
            )
        )
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "enrollment", AuditLog.action.in_(("advance", "transition"))
            )
        )
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def test_a_fresh_enrollment_is_due_immediately(campaign_with_steps: int, client_row: int) -> None:
    with SessionLocal() as session:
        _make_enrollment(session, campaign_id=campaign_with_steps, client_id=client_row)

    with SessionLocal() as session:
        due = select_due_enrollments(session, campaign_id=campaign_with_steps)
    assert [row.client_id for row in due] == [client_row]


def test_a_suppressed_primary_row_is_never_selected_as_due(
    campaign_with_steps: int, client_row: int
) -> None:
    """A row that lost the primary-contact tiebreak stays enrolled but is
    never due: generating from it would double-touch the person it shares
    with the primary row."""
    with SessionLocal() as session:
        _make_enrollment(
            session,
            campaign_id=campaign_with_steps,
            client_id=client_row,
            is_primary_contact_row=False,
        )

    with SessionLocal() as session:
        due = select_due_enrollments(session, campaign_id=campaign_with_steps)
    assert due == []


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


# --- stale contacts sort last, they are never excluded ---


@pytest.fixture
def three_clients(db: None):
    """One fresh, one stale, one with no feature row at all yet."""
    fund_id = 99199
    fresh_id, stale_id, unknown_id = 99010, 99011, 99012
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund"))
        session.commit()
        for client_id in (fresh_id, stale_id, unknown_id):
            session.add(
                Clients(
                    client_id=client_id,
                    unit_fund_id=fund_id,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                )
            )
        session.commit()
        session.add(ClientFeatures(client_id=fresh_id, stale_contact=False))
        session.add(ClientFeatures(client_id=stale_id, stale_contact=True))
        # unknown_id deliberately gets no ClientFeatures row.
        session.commit()

    yield fresh_id, stale_id, unknown_id

    with SessionLocal() as session:
        ids = (fresh_id, stale_id, unknown_id)
        session.execute(delete(Enrollment).where(Enrollment.client_id.in_(ids)))
        session.execute(delete(ClientFeatures).where(ClientFeatures.client_id.in_(ids)))
        session.execute(delete(Clients).where(Clients.client_id.in_(ids)))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_a_stale_contact_sorts_after_a_fresh_one(
    campaign_with_steps: int, three_clients: tuple[int, int, int]
) -> None:
    fresh_id, stale_id, unknown_id = three_clients
    with SessionLocal() as session:
        # Enrolled stale first, so ordering can only be the ordering logic,
        # not just insertion order.
        _make_enrollment(session, campaign_id=campaign_with_steps, client_id=stale_id)
        _make_enrollment(session, campaign_id=campaign_with_steps, client_id=fresh_id)
        _make_enrollment(session, campaign_id=campaign_with_steps, client_id=unknown_id)

    with SessionLocal() as session:
        due = select_due_enrollments(session, campaign_id=campaign_with_steps)
    assert due[-1].client_id == stale_id
    assert {row.client_id for row in due[:-1]} == {fresh_id, unknown_id}


def test_a_client_with_no_feature_row_is_treated_as_fresh_not_excluded(
    campaign_with_steps: int, three_clients: tuple[int, int, int]
) -> None:
    """No signal at all must never look like a hold; it must sort as fresh."""
    _fresh_id, _stale_id, unknown_id = three_clients
    with SessionLocal() as session:
        _make_enrollment(session, campaign_id=campaign_with_steps, client_id=unknown_id)

    with SessionLocal() as session:
        due = select_due_enrollments(session, campaign_id=campaign_with_steps)
    assert [row.client_id for row in due] == [unknown_id]


def test_stale_contact_is_never_excluded_from_the_due_batch(
    campaign_with_steps: int, three_clients: tuple[int, int, int]
) -> None:
    fresh_id, stale_id, unknown_id = three_clients
    with SessionLocal() as session:
        for client_id in (fresh_id, stale_id, unknown_id):
            _make_enrollment(session, campaign_id=campaign_with_steps, client_id=client_id)

    with SessionLocal() as session:
        due = select_due_enrollments(session, campaign_id=campaign_with_steps)
    assert len(due) == 3


def test_count_stale_contacts_counts_only_the_stale_ones(
    campaign_with_steps: int, three_clients: tuple[int, int, int]
) -> None:
    fresh_id, stale_id, unknown_id = three_clients
    with SessionLocal() as session:
        enrollments = [
            _make_enrollment(session, campaign_id=campaign_with_steps, client_id=client_id)
            for client_id in (fresh_id, stale_id, unknown_id)
        ]
        assert count_stale_contacts(session, enrollments) == 1


def test_count_stale_contacts_of_an_empty_list_is_zero(db: None) -> None:
    with SessionLocal() as session:
        assert count_stale_contacts(session, []) == 0


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


def test_a_step_already_touched_but_not_yet_sent_is_excluded_from_the_next_page(
    campaign_with_steps: int, three_clients: tuple[int, int, int]
) -> None:
    """A touch_log row for the next step means that step is already being
    worked (generated, or waiting on review) -- select_due_enrollments must
    not keep handing it back every page, or it would permanently occupy the
    front of a large batch and starve everything enrolled after it."""
    fresh_id, _stale_id, unknown_id = three_clients
    with SessionLocal() as session:
        touched = _make_enrollment(session, campaign_id=campaign_with_steps, client_id=fresh_id)
        untouched = _make_enrollment(session, campaign_id=campaign_with_steps, client_id=unknown_id)
        session.add(TouchLog(enrollment_id=touched.enrollment_id, step_no=1))
        session.commit()
        untouched_id = untouched.enrollment_id

    try:
        with SessionLocal() as session:
            due = select_due_enrollments(session, campaign_id=campaign_with_steps)
        assert [row.enrollment_id for row in due] == [untouched_id]
    finally:
        with SessionLocal() as session:
            session.execute(delete(TouchLog).where(TouchLog.enrollment_id == touched.enrollment_id))
            session.commit()


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
            session,
            campaign_id=campaign_with_steps,
            client_id=client_row,
            current_step=3,
            status="in_progress",
        )
        advance_enrollment(session, enrollment, step_no=4, sent_at=datetime.now(UTC))
        session.commit()
        enrollment_id = enrollment.enrollment_id

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment_id)
        assert row.current_step == 4
        assert row.status == "completed"
        assert row.next_due_at is None


def test_advance_enrollment_completes_a_one_step_campaigns_first_touch(
    one_step_campaign: int, client_row: int
) -> None:
    """A fresh enrollment (status enrolled) whose only step is also its last
    must still pass through in_progress: the state machine has no direct
    enrolled-to-completed move."""
    with SessionLocal() as session:
        enrollment = _make_enrollment(session, campaign_id=one_step_campaign, client_id=client_row)
        assert enrollment.status == "enrolled"
        advance_enrollment(session, enrollment, step_no=1, sent_at=datetime.now(UTC))
        session.commit()
        enrollment_id = enrollment.enrollment_id

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment_id)
        assert row.current_step == 1
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
