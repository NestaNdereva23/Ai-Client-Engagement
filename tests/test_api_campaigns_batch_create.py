from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.services.campaigns as campaigns_service
from app.db.models.campaigns import Enrollment
from app.db.models.models import ClientFeatures, Clients, Funds, PiiVault
from app.db.models.outreach import Campaign
from app.db.models.rules import ClientMessageIndicators
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

CAMPAIGNS = "/api/v1/campaigns"
FUND_ID = 97790


@pytest.fixture
def batch_cohort_clients(db: None):
    matching = 97791
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Batch Create Test Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=matching,
                unit_fund_id=FUND_ID,
                n_purchases_returned=0,
                n_sales_returned=0,
                total_purchase_amount=60_000.0,
            )
        )
        session.commit()
        session.add(
            ClientFeatures(client_id=matching, value_band="Medium", purchase_depth="capped")
        )
        session.add(
            ClientMessageIndicators(
                client_id=matching,
                message_angle="pick_up_again",
                urgency="normal",
                priority_tier="T2",
                prompt_variant="pick_up_again",
                rule_name="batch_create_test",
                rule_version=1,
            )
        )
        session.add(PiiVault(client_id=matching, client_name="Batch Create Person"))
        session.commit()

    yield matching

    with SessionLocal() as session:
        campaign_ids = session.scalars(
            select(Campaign.campaign_id).where(Campaign.name.like("batch create test:%"))
        ).all()
        if campaign_ids:
            session.execute(delete(Enrollment).where(Enrollment.campaign_id.in_(campaign_ids)))
            session.execute(delete(Campaign).where(Campaign.campaign_id.in_(campaign_ids)))
        session.execute(
            delete(ClientMessageIndicators).where(ClientMessageIndicators.client_id == matching)
        )
        session.execute(delete(ClientFeatures).where(ClientFeatures.client_id == matching))
        session.execute(delete(PiiVault).where(PiiVault.client_id == matching))
        session.execute(delete(Clients).where(Clients.client_id == matching))
        session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
        session.commit()


def test_campaign_batch_creates_one_campaign_per_angle(batch_cohort_clients) -> None:
    matching = batch_cohort_clients
    response = client.post(
        f"{CAMPAIGNS}/batch",
        json={
            "name": "batch create test",
            "cohort": {"fund_id": FUND_ID, "value_band": "Medium"},
            "angles": ["pick_up_again", "unmatched_angle"],
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["failed"] == []
    assert len(body["created"]) == 2

    by_angle = {c["cohort_definition"]["message_angle"]: c for c in body["created"]}
    assert by_angle["pick_up_again"]["enrolled_count"] == 1
    assert by_angle["pick_up_again"]["name"] == "batch create test: pick_up_again"
    assert by_angle["unmatched_angle"]["enrolled_count"] == 0

    with SessionLocal() as session:
        enrolled = session.scalars(
            select(Enrollment.client_id).where(
                Enrollment.campaign_id == by_angle["pick_up_again"]["campaign_id"]
            )
        ).all()
    assert list(enrolled) == [matching]


def test_campaign_batch_rejects_no_angles(db: None) -> None:
    response = client.post(
        f"{CAMPAIGNS}/batch",
        json={"name": "batch create test empty", "cohort": {"fund_id": FUND_ID}, "angles": []},
    )
    assert response.status_code == 422


def test_campaign_batch_isolates_a_failure_to_its_own_angle(
    batch_cohort_clients, monkeypatch
) -> None:
    original_create_campaign = campaigns_service.create_campaign

    def fake_create_campaign(
        session, *, name, campaign_type, cohort_filters, start_date=None, end_date=None
    ):
        if cohort_filters.get("message_angle") == "boom_angle":
            raise ValueError("simulated failure")
        return original_create_campaign(
            session,
            name=name,
            campaign_type=campaign_type,
            cohort_filters=cohort_filters,
            start_date=start_date,
            end_date=end_date,
        )

    monkeypatch.setattr(campaigns_service, "create_campaign", fake_create_campaign)

    response = client.post(
        f"{CAMPAIGNS}/batch",
        json={
            "name": "batch create test",
            "cohort": {"fund_id": FUND_ID, "value_band": "Medium"},
            "angles": ["pick_up_again", "boom_angle"],
        },
    )
    assert response.status_code == 201
    body = response.json()

    assert len(body["created"]) == 1
    assert body["created"][0]["cohort_definition"]["message_angle"] == "pick_up_again"

    assert len(body["failed"]) == 1
    assert body["failed"][0]["angle"] == "boom_angle"
    assert "simulated failure" in body["failed"][0]["error"]

    with SessionLocal() as session:
        surviving = session.scalars(
            select(Campaign.campaign_id).where(Campaign.name.like("batch create test:%"))
        ).all()
    assert len(surviving) == 1
