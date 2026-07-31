from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.db.models.campaigns import CampaignStep, ContactEvent, Enrollment, TouchLog
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign
from app.db.session import SessionLocal


@pytest.fixture
def campaign(db: None):
    with SessionLocal() as session:
        row = Campaign(
            name="test campaign",
            cohort_definition={"archetype": "dormant"},
            start_date=date(2026, 1, 1),
            end_date=date(2026, 2, 1),
        )
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id

    yield campaign_id

    with SessionLocal() as session:
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


@pytest.fixture
def client_row(db: None):
    fund_id = 962
    client_id = 96201
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
        # Torn down before `campaign` (fixtures unwind in reverse setup order), so
        # any enrollment/touch referencing this client must go first or the FK blocks it.
        enrollment_ids = session.scalars(
            select(Enrollment.enrollment_id).where(Enrollment.client_id == client_id)
        ).all()
        if enrollment_ids:
            session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
        session.execute(delete(Enrollment).where(Enrollment.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_campaign_carries_cohort_and_scheduling_columns(campaign: int) -> None:
    with SessionLocal() as session:
        row = session.get(Campaign, campaign)
        assert row.cohort_definition == {"archetype": "dormant"}
        assert row.start_date == date(2026, 1, 1)
        assert row.end_date == date(2026, 2, 1)


def test_campaign_step_unique_per_campaign_and_step_no(campaign: int) -> None:
    with SessionLocal() as session:
        session.add(
            CampaignStep(
                campaign_id=campaign, step_no=1, offset_days=0, message_angle="winback_habit"
            )
        )
        session.commit()

    with SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(
            CampaignStep(
                campaign_id=campaign, step_no=1, offset_days=7, message_angle="winback_value"
            )
        )
        session.commit()


def test_enrollment_defaults_to_enrolled_status(campaign: int, client_row: int) -> None:
    with SessionLocal() as session:
        row = Enrollment(campaign_id=campaign, client_id=client_row)
        session.add(row)
        session.commit()
        assert row.status == "enrolled"
        assert row.current_step == 0


def test_enrollment_status_check_constraint_rejects_an_invalid_value(
    campaign: int, client_row: int
) -> None:
    with SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(
            Enrollment(campaign_id=campaign, client_id=client_row, status="not_a_real_status")
        )
        session.commit()


def test_enrollment_is_unique_per_campaign_and_client(campaign: int, client_row: int) -> None:
    with SessionLocal() as session:
        session.add(Enrollment(campaign_id=campaign, client_id=client_row))
        session.commit()

    with SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(Enrollment(campaign_id=campaign, client_id=client_row))
        session.commit()


def test_touch_log_is_the_idempotency_key_for_a_repeated_touch(
    campaign: int, client_row: int
) -> None:
    with SessionLocal() as session:
        enrollment = Enrollment(campaign_id=campaign, client_id=client_row)
        session.add(enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

        session.add(TouchLog(enrollment_id=enrollment_id, step_no=1))
        session.commit()

    with SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(TouchLog(enrollment_id=enrollment_id, step_no=1))
        session.commit()


def test_contact_events_type_check_constraint_rejects_an_invalid_value(db: None) -> None:
    with SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(
            ContactEvent(client_id=96201, type="not_a_real_type", occurred_at=datetime.now(UTC))
        )
        session.commit()


def test_contact_events_round_trips_a_reply(db: None) -> None:
    occurred_at = datetime.now(UTC)
    with SessionLocal() as session:
        row = ContactEvent(client_id=96201, type="reply", occurred_at=occurred_at)
        session.add(row)
        session.commit()
        event_id = row.id

    with SessionLocal() as session:
        stored = session.get(ContactEvent, event_id)
        assert stored.client_id == 96201
        assert stored.type == "reply"

    with SessionLocal() as session:
        session.execute(delete(ContactEvent).where(ContactEvent.id == event_id))
        session.commit()
