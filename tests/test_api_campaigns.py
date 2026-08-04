"""The campaign console API: enrollment totals, including primary-row suppression."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.campaigns import Enrollment
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

CAMPAIGNS = "/api/v1/campaigns"


@pytest.fixture
def campaign_with_a_suppressed_row(db: None):
    """One campaign, two client_ids for the same person: one primary, one suppressed."""
    fund_id = 97701
    primary_id, suppressed_id = 97710, 97711
    with SessionLocal() as session:
        row = Campaign(name="test summary campaign")
        session.add(row)
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund"))
        session.commit()
        campaign_id = row.campaign_id

        for client_id in (primary_id, suppressed_id):
            session.add(
                Clients(
                    client_id=client_id,
                    unit_fund_id=fund_id,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                )
            )
        session.add_all(
            [
                PiiVault(client_id=primary_id, client_name="Same Person"),
                PiiVault(client_id=suppressed_id, client_name="Same Person"),
            ]
        )
        session.commit()
        session.add_all(
            [
                Enrollment(
                    campaign_id=campaign_id,
                    client_id=primary_id,
                    is_primary_contact_row=True,
                ),
                Enrollment(
                    campaign_id=campaign_id,
                    client_id=suppressed_id,
                    is_primary_contact_row=False,
                ),
            ]
        )
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_((primary_id, suppressed_id))))
        session.execute(delete(Clients).where(Clients.client_id.in_((primary_id, suppressed_id))))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_campaign_summary_counts_the_suppressed_row(campaign_with_a_suppressed_row) -> None:
    campaign_id = campaign_with_a_suppressed_row
    response = client.get(f"{CAMPAIGNS}/{campaign_id}/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["total_enrolled"] == 2
    assert body["primary_count"] == 1
    assert body["suppressed_count"] == 1


def test_campaign_summary_404s_for_an_unknown_campaign(db: None) -> None:
    response = client.get(f"{CAMPAIGNS}/999999999/summary")
    assert response.status_code == 404
