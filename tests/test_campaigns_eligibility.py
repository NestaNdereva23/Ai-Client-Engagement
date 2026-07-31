"""The eligibility gate: every skip condition, and that a skip both logs a
reason and moves the enrollment to the right state.

Each test builds its own client and enrollment so the checks stay isolated
from each other; a shared campaign with a single step covers the common
case, and the terminal-vs-transient split is exercised by checking whether
enrollment.status changed after a skip.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.campaigns.eligibility import check_eligibility
from app.db.models.audit import AuditLog
from app.db.models.campaigns import CampaignStep, ContactEvent, Enrollment, TouchLog
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign
from app.db.models.suppression import Suppression
from app.db.session import SessionLocal

_FUND_ID = 995


@pytest.fixture
def campaign_with_step(db: None):
    """Two steps, so a mid-sequence enrollment (current_step=1) still has a
    next step to be gated on rather than tripping the no-more-steps check."""
    with SessionLocal() as session:
        campaign = Campaign(name="test eligibility campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id
        session.add(
            CampaignStep(
                campaign_id=campaign_id, step_no=1, offset_days=0, message_angle="winback_habit"
            )
        )
        session.add(
            CampaignStep(
                campaign_id=campaign_id, step_no=2, offset_days=7, message_angle="winback_value"
            )
        )
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "enrollment", AuditLog.action == "gate_skip"
            )
        )
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


@pytest.fixture
def fund(db: None):
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=_FUND_ID, unit_fund_name="Test Fund"))
        session.commit()

    yield _FUND_ID

    with SessionLocal() as session:
        session.execute(delete(Funds).where(Funds.unit_fund_id == _FUND_ID))
        session.commit()


def _make_client(session, client_id: int, *, contact_email: str | None = "a@example.com") -> None:
    session.add(
        Clients(
            client_id=client_id,
            unit_fund_id=_FUND_ID,
            n_purchases_returned=0,
            n_sales_returned=0,
        )
    )
    session.add(
        PiiVault(client_id=client_id, client_name="Test Client", contact_email=contact_email)
    )


def _make_enrollment(session, *, campaign_id: int, client_id: int, **overrides) -> Enrollment:
    row = Enrollment(campaign_id=campaign_id, client_id=client_id, **overrides)
    session.add(row)
    session.commit()
    return row


def _cleanup_client(client_id: int) -> None:
    with SessionLocal() as session:
        session.execute(delete(ContactEvent).where(ContactEvent.client_id == client_id))
        session.execute(delete(Suppression).where(Suppression.client_id == client_id))
        enrollment_ids = session.scalars(
            select(Enrollment.enrollment_id).where(Enrollment.client_id == client_id)
        ).all()
        if enrollment_ids:
            session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
        session.execute(delete(Enrollment).where(Enrollment.client_id == client_id))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.commit()


def test_an_otherwise_clean_enrollment_is_eligible(campaign_with_step: int, fund: int) -> None:
    client_id = 99501
    with SessionLocal() as session:
        _make_client(session, client_id)
        session.commit()
        enrollment = _make_enrollment(session, campaign_id=campaign_with_step, client_id=client_id)

        result = check_eligibility(session, enrollment)

    assert result.eligible is True
    _cleanup_client(client_id)


def test_a_suppressed_client_is_skipped_and_excluded_before_the_first_touch(
    campaign_with_step: int, fund: int
) -> None:
    client_id = 99502
    with SessionLocal() as session:
        _make_client(session, client_id)
        session.add(Suppression(client_id=client_id, reason="unsubscribe"))
        session.commit()
        enrollment = _make_enrollment(session, campaign_id=campaign_with_step, client_id=client_id)

        result = check_eligibility(session, enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    assert result.reason == "suppressed"
    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment_id).status == "excluded"
    _cleanup_client(client_id)


def test_a_suppressed_client_mid_sequence_lands_on_the_bounce_or_optout_state(
    campaign_with_step: int, fund: int
) -> None:
    client_id = 99503
    with SessionLocal() as session:
        _make_client(session, client_id)
        session.add(Suppression(client_id=client_id, reason="hard bounce"))
        session.commit()
        enrollment = _make_enrollment(
            session,
            campaign_id=campaign_with_step,
            client_id=client_id,
            current_step=1,
            status="in_progress",
        )

        check_eligibility(session, enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment_id).status == "stopped_bounce"
    _cleanup_client(client_id)


def test_an_opted_out_client_is_skipped_and_stopped(campaign_with_step: int, fund: int) -> None:
    client_id = 99504
    with SessionLocal() as session:
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=_FUND_ID,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.add(
            PiiVault(
                client_id=client_id,
                client_name="Test Client",
                contact_email="a@example.com",
                opt_out_flag=True,
            )
        )
        session.commit()
        enrollment = _make_enrollment(
            session,
            campaign_id=campaign_with_step,
            client_id=client_id,
            current_step=1,
            status="in_progress",
        )

        result = check_eligibility(session, enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    assert result.reason == "opted_out"
    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment_id).status == "stopped_optout"
    _cleanup_client(client_id)


def test_a_client_with_no_deliverable_contact_is_skipped_and_excluded(
    campaign_with_step: int, fund: int
) -> None:
    client_id = 99505
    with SessionLocal() as session:
        _make_client(session, client_id, contact_email=None)
        session.commit()
        enrollment = _make_enrollment(session, campaign_id=campaign_with_step, client_id=client_id)

        result = check_eligibility(session, enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    assert result.reason == "no_deliverable_contact"
    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment_id).status == "excluded"
    _cleanup_client(client_id)


def test_a_client_within_the_cooldown_window_is_skipped_without_a_status_change(
    campaign_with_step: int, fund: int
) -> None:
    """Cooldown is global: a recent touch from an unrelated campaign is
    enough to block this one, even though this enrollment has no touch of
    its own yet."""
    client_id = 99506
    with SessionLocal() as session:
        other_campaign = Campaign(name="other cooldown campaign")
        session.add(other_campaign)
        session.commit()
        other_campaign_id = other_campaign.campaign_id

        _make_client(session, client_id)
        session.commit()
        other_enrollment = _make_enrollment(
            session, campaign_id=other_campaign_id, client_id=client_id
        )
        session.add(
            TouchLog(
                enrollment_id=other_enrollment.enrollment_id,
                step_no=1,
                sent_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        session.commit()

        enrollment = _make_enrollment(session, campaign_id=campaign_with_step, client_id=client_id)
        result = check_eligibility(session, enrollment, cooldown_days=7)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    assert result.reason == "cooldown"
    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment_id).status == "enrolled"

    _cleanup_client(client_id)
    with SessionLocal() as session:
        session.execute(delete(Campaign).where(Campaign.campaign_id == other_campaign_id))
        session.commit()


def test_a_reply_since_the_last_touch_stops_the_sequence(
    campaign_with_step: int, fund: int
) -> None:
    """current_step=1 with no touch_log row of its own: enough is known from
    current_step alone to gate step 2, and enrolled_at stands in for "since
    the last touch" once a reply lands after it."""
    client_id = 99507
    with SessionLocal() as session:
        _make_client(session, client_id)
        session.commit()
        enrolled_at = datetime.now(UTC) - timedelta(days=30)
        enrollment = _make_enrollment(
            session,
            campaign_id=campaign_with_step,
            client_id=client_id,
            current_step=1,
            status="in_progress",
            enrolled_at=enrolled_at,
        )
        session.add(
            ContactEvent(
                client_id=client_id, type="reply", occurred_at=enrolled_at + timedelta(hours=1)
            )
        )
        session.commit()

        result = check_eligibility(session, enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    assert result.reason == "replied"
    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment_id).status == "stopped_reply"
    _cleanup_client(client_id)


def test_a_bounce_event_stops_the_sequence(campaign_with_step: int, fund: int) -> None:
    client_id = 99508
    with SessionLocal() as session:
        _make_client(session, client_id)
        session.commit()
        enrollment = _make_enrollment(
            session,
            campaign_id=campaign_with_step,
            client_id=client_id,
            current_step=1,
            status="in_progress",
        )
        session.add(ContactEvent(client_id=client_id, type="bounce", occurred_at=datetime.now(UTC)))
        session.commit()

        result = check_eligibility(session, enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    assert result.reason == "bounce"
    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment_id).status == "stopped_bounce"
    _cleanup_client(client_id)


def test_new_activity_reengages_the_client_and_stops_the_sequence(
    campaign_with_step: int, fund: int
) -> None:
    client_id = 99509
    with SessionLocal() as session:
        _make_client(session, client_id)
        session.commit()
        enrollment = _make_enrollment(
            session,
            campaign_id=campaign_with_step,
            client_id=client_id,
            current_step=1,
            status="in_progress",
            enrolled_at=datetime.now(UTC) - timedelta(days=10),
        )
        client = session.get(Clients, client_id)
        client.last_activity_date = date.today()
        session.commit()

        result = check_eligibility(session, enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    assert result.reason == "reengaged"
    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment_id).status == "stopped_reengaged"
    _cleanup_client(client_id)


def test_a_paused_campaign_is_skipped_without_a_status_change(db: None, fund: int) -> None:
    client_id = 99510
    with SessionLocal() as session:
        campaign = Campaign(name="paused campaign", status="paused")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id
        session.add(
            CampaignStep(
                campaign_id=campaign_id, step_no=1, offset_days=0, message_angle="winback_habit"
            )
        )
        session.commit()

        _make_client(session, client_id)
        session.commit()
        enrollment = _make_enrollment(session, campaign_id=campaign_id, client_id=client_id)

        result = check_eligibility(session, enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    assert result.reason == "campaign_inactive"
    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment_id).status == "enrolled"

    _cleanup_client(client_id)
    with SessionLocal() as session:
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def test_an_already_touched_step_is_skipped_without_a_status_change(
    campaign_with_step: int, fund: int
) -> None:
    client_id = 99511
    with SessionLocal() as session:
        _make_client(session, client_id)
        session.commit()
        enrollment = _make_enrollment(session, campaign_id=campaign_with_step, client_id=client_id)
        session.add(TouchLog(enrollment_id=enrollment.enrollment_id, step_no=1))
        session.commit()

        result = check_eligibility(session, enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    assert result.reason == "already_touched"
    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment_id).status == "enrolled"
    _cleanup_client(client_id)


def test_a_touch_still_waiting_on_a_message_blocks_the_next_step(
    campaign_with_step: int, fund: int
) -> None:
    """Step 1 was logged but never got a message, so step 2 must wait
    rather than being generated on top of it."""
    client_id = 99512
    with SessionLocal() as session:
        _make_client(session, client_id)
        session.commit()
        enrollment = _make_enrollment(
            session,
            campaign_id=campaign_with_step,
            client_id=client_id,
            current_step=1,
            status="in_progress",
        )
        session.add(TouchLog(enrollment_id=enrollment.enrollment_id, step_no=1, message_id=None))
        session.commit()

        result = check_eligibility(session, enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    assert result.reason == "previous_touch_pending"
    with SessionLocal() as session:
        assert session.get(Enrollment, enrollment_id).status == "in_progress"

    _cleanup_client(client_id)
