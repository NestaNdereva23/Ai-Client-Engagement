from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db.models.campaigns import Enrollment
from app.db.models.models import ClientFeatures, Clients, Funds, PiiVault
from app.db.models.outreach import Campaign
from app.db.models.rules import ClientMessageIndicators
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

CAMPAIGNS = "/api/v1/campaigns"
FUND_ID = 97780


@pytest.fixture
def preview_cohort_clients(db: None):
    matching_a, matching_b, non_matching = 97781, 97782, 97783
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Preview API Test Fund"))
        session.commit()
        for client_id, amount in (
            (matching_a, 80_000.0),
            (matching_b, 40_000.0),
            (non_matching, 10_000.0),
        ):
            session.add(
                Clients(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                    total_purchase_amount=amount,
                )
            )
        session.commit()
        session.add_all(
            [
                ClientFeatures(client_id=matching_a, value_band="Medium", purchase_depth="capped"),
                ClientFeatures(client_id=matching_b, value_band="Medium", purchase_depth="capped"),
                ClientFeatures(client_id=non_matching, value_band="Low", purchase_depth="capped"),
            ]
        )
        session.add(
            ClientMessageIndicators(
                client_id=matching_a,
                message_angle="pick_up_again",
                urgency="normal",
                priority_tier="T2",
                prompt_variant="pick_up_again",
                rule_name="preview_api_test",
                rule_version=1,
            )
        )
        session.add_all(
            [
                PiiVault(client_id=matching_a, client_name="Preview API Alpha"),
                PiiVault(client_id=matching_b, client_name="Preview API Beta"),
                PiiVault(client_id=non_matching, client_name="Preview API Gamma"),
            ]
        )
        session.commit()

    yield matching_a, matching_b, non_matching

    with SessionLocal() as session:
        session.execute(
            delete(ClientMessageIndicators).where(
                ClientMessageIndicators.client_id.in_((matching_a, matching_b, non_matching))
            )
        )
        session.execute(
            delete(ClientFeatures).where(
                ClientFeatures.client_id.in_((matching_a, matching_b, non_matching))
            )
        )
        session.execute(
            delete(PiiVault).where(PiiVault.client_id.in_((matching_a, matching_b, non_matching)))
        )
        session.execute(
            delete(Clients).where(Clients.client_id.in_((matching_a, matching_b, non_matching)))
        )
        session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
        session.commit()


def test_campaign_preview_returns_the_real_matched_count(preview_cohort_clients) -> None:
    response = client.post(
        f"{CAMPAIGNS}/preview",
        json={"fund_id": FUND_ID, "value_band": "Medium"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["matched_count"] == 2
    assert body["primary_count"] == 2
    assert body["suppressed_count"] == 0
    assert body["valued_count"] == 2
    assert body["estimated_value"] == 120_000.0


def test_campaign_preview_rejects_an_empty_cohort(db: None) -> None:
    response = client.post(f"{CAMPAIGNS}/preview", json={})
    assert response.status_code == 422


def test_campaign_preview_does_not_create_a_campaign(preview_cohort_clients) -> None:
    before = client.get(CAMPAIGNS, params={"limit": 200}).json()["items"]

    response = client.post(
        f"{CAMPAIGNS}/preview",
        json={"fund_id": FUND_ID, "value_band": "Medium"},
    )
    assert response.status_code == 200

    after = client.get(CAMPAIGNS, params={"limit": 200}).json()["items"]
    assert len(after) == len(before)

    matching_a, matching_b, _ = preview_cohort_clients
    with SessionLocal() as session:
        enrolled = session.scalars(
            select(Enrollment.client_id).where(Enrollment.client_id.in_((matching_a, matching_b)))
        ).all()
    assert list(enrolled) == []


def test_campaign_preview_batch_scopes_each_angle(preview_cohort_clients) -> None:
    response = client.post(
        f"{CAMPAIGNS}/preview/batch",
        json={
            "fund_id": FUND_ID,
            "value_band": "Medium",
            "angles": ["pick_up_again", "back_on_schedule"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["narrow"]["matched_count"] == 2
    assert body["narrow"]["estimated_value"] == 120_000.0

    by_angle = {a["message_angle"]: a for a in body["angles"]}
    assert by_angle["pick_up_again"]["matched_count"] == 1
    assert by_angle["pick_up_again"]["estimated_value"] == 80_000.0
    assert by_angle["back_on_schedule"]["matched_count"] == 0
    assert by_angle["back_on_schedule"]["estimated_value"] == 0


def test_campaign_preview_batch_with_no_angles_lists_none(preview_cohort_clients) -> None:
    response = client.post(
        f"{CAMPAIGNS}/preview/batch",
        json={"fund_id": FUND_ID, "value_band": "Medium"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["narrow"]["matched_count"] == 2
    assert body["angles"] == []


@pytest.fixture
def campaigns_with_different_cohorts(db: None):
    with SessionLocal() as session:
        rows = [
            Campaign(
                name="cohort filter test A",
                cohort_definition={
                    "value_band": "Medium",
                    "purchase_depth": "capped",
                    "message_angle": "pick_up_again",
                },
            ),
            Campaign(
                name="cohort filter test B",
                cohort_definition={
                    "value_band": "Medium",
                    "purchase_depth": "capped",
                    "message_angle": "your_next_deposit",
                },
            ),
            Campaign(
                name="cohort filter test C",
                cohort_definition={"value_band": "High"},
            ),
            Campaign(
                name="cohort filter test D",
                cohort_definition={"value_band": "High", "newly_dormant": True},
            ),
        ]
        session.add_all(rows)
        session.commit()
        campaign_ids = [row.campaign_id for row in rows]

    yield campaign_ids

    with SessionLocal() as session:
        session.execute(delete(Campaign).where(Campaign.campaign_id.in_(campaign_ids)))
        session.commit()


def test_list_campaigns_filters_on_the_full_cohort_combination(
    campaigns_with_different_cohorts,
) -> None:
    campaign_a, campaign_b, campaign_c, campaign_d = campaigns_with_different_cohorts
    response = client.get(
        CAMPAIGNS,
        params={
            "value_band": "Medium",
            "purchase_depth": "capped",
            "message_angle": "pick_up_again",
            "limit": 200,
        },
    )
    assert response.status_code == 200
    ids = {row["campaign_id"] for row in response.json()["items"]}
    assert ids == {campaign_a}


def test_list_campaigns_filters_on_a_single_cohort_field(
    campaigns_with_different_cohorts,
) -> None:
    campaign_a, campaign_b, campaign_c, campaign_d = campaigns_with_different_cohorts
    response = client.get(CAMPAIGNS, params={"value_band": "Medium", "limit": 200})
    assert response.status_code == 200
    ids = {row["campaign_id"] for row in response.json()["items"]}
    assert ids == {campaign_a, campaign_b}
    assert campaign_c not in ids
    assert campaign_d not in ids


def test_list_campaigns_filters_on_a_boolean_cohort_field(
    campaigns_with_different_cohorts,
) -> None:
    campaign_a, campaign_b, campaign_c, campaign_d = campaigns_with_different_cohorts
    response = client.get(
        CAMPAIGNS, params={"value_band": "High", "newly_dormant": "true", "limit": 200}
    )
    assert response.status_code == 200
    ids = {row["campaign_id"] for row in response.json()["items"]}
    assert ids == {campaign_d}
    assert campaign_c not in ids


def test_list_campaigns_with_no_cohort_params_is_unaffected(
    campaigns_with_different_cohorts,
) -> None:
    campaign_a, campaign_b, campaign_c, campaign_d = campaigns_with_different_cohorts
    response = client.get(CAMPAIGNS, params={"limit": 200})
    assert response.status_code == 200
    ids = {row["campaign_id"] for row in response.json()["items"]}
    assert {campaign_a, campaign_b, campaign_c, campaign_d} <= ids
