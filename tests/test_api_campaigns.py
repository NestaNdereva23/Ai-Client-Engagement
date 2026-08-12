"""The campaign console API: enrollment totals, including primary-row suppression."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db.models.campaigns import CampaignStep, Enrollment
from app.db.models.models import ClientFeatures, Clients, Funds, PiiVault
from app.db.models.outreach import Campaign
from app.db.models.template_generation_plan import TemplateGenerationPlan
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


def test_list_campaigns_carries_its_own_enrollment_counts(
    campaign_with_a_suppressed_row,
) -> None:
    campaign_id = campaign_with_a_suppressed_row
    response = client.get(CAMPAIGNS, params={"limit": 200})
    assert response.status_code == 200
    rows = {row["campaign_id"]: row for row in response.json()["items"]}
    assert campaign_id in rows
    row = rows[campaign_id]
    assert row["total_enrolled"] == 2
    assert row["primary_count"] == 1
    assert row["suppressed_count"] == 1
    assert row["name"] == "test summary campaign"


@pytest.fixture
def cohort_clients(db: None):
    """Two clients matching a cohort filter, one that doesn't."""
    fund_id = 97702
    matching_a, matching_b, non_matching = 97720, 97721, 97722
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund"))
        session.commit()
        for client_id in (matching_a, matching_b, non_matching):
            session.add(
                Clients(
                    client_id=client_id,
                    unit_fund_id=fund_id,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                )
            )
        session.commit()
        session.add_all(
            [
                ClientFeatures(client_id=matching_a, value_band="High"),
                ClientFeatures(client_id=matching_b, value_band="High"),
                ClientFeatures(client_id=non_matching, value_band="Low"),
            ]
        )
        session.commit()

    yield fund_id, matching_a, matching_b, non_matching

    with SessionLocal() as session:
        campaign_ids = session.scalars(
            select(Campaign.campaign_id).where(Campaign.name == "cohort test campaign")
        ).all()
        if campaign_ids:
            session.execute(delete(Enrollment).where(Enrollment.campaign_id.in_(campaign_ids)))
            session.execute(delete(Campaign).where(Campaign.campaign_id.in_(campaign_ids)))
        session.execute(
            delete(ClientFeatures).where(
                ClientFeatures.client_id.in_((matching_a, matching_b, non_matching))
            )
        )
        session.execute(
            delete(Clients).where(Clients.client_id.in_((matching_a, matching_b, non_matching)))
        )
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_create_campaign_enrolls_exactly_the_matching_cohort(cohort_clients) -> None:
    fund_id, matching_a, matching_b, non_matching = cohort_clients
    response = client.post(
        CAMPAIGNS,
        json={
            "name": "cohort test campaign",
            "cohort": {"fund_id": fund_id, "value_band": "High"},
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["enrolled_count"] == 2
    assert body["cohort_definition"] == {
        "fund_id": fund_id,
        "value_band": "High",
        "recency_band": None,
        "purchase_depth": None,
        "message_angle": None,
        "newly_dormant": None,
    }

    with SessionLocal() as session:
        enrolled_ids = set(
            session.scalars(
                select(Enrollment.client_id).where(Enrollment.campaign_id == body["campaign_id"])
            ).all()
        )
    assert enrolled_ids == {matching_a, matching_b}
    assert non_matching not in enrolled_ids


def test_create_campaign_rejects_an_empty_cohort(db: None) -> None:
    response = client.post(CAMPAIGNS, json={"name": "empty cohort campaign", "cohort": {}})
    assert response.status_code == 422


def test_create_campaign_accepts_newly_dormant_false_as_a_real_filter(db: None) -> None:
    """newly_dormant=False excludes the newly dormant; it is not an absent filter."""
    response = client.post(
        CAMPAIGNS,
        json={"name": "not newly dormant campaign", "cohort": {"newly_dormant": False}},
    )
    assert response.status_code == 201


@pytest.fixture
def bare_campaign(db: None):
    """A campaign with no steps yet, for exercising step creation on its own."""
    with SessionLocal() as session:
        row = Campaign(name="step creation test campaign")
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id

    yield campaign_id

    with SessionLocal() as session:
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def test_post_campaign_step_assigns_sequential_step_numbers(bare_campaign: int) -> None:
    first = client.post(
        f"{CAMPAIGNS}/{bare_campaign}/steps",
        json={"offset_days": 0, "message_angle": "winback_habit"},
    )
    assert first.status_code == 201
    assert first.json()["step_no"] == 1

    second = client.post(
        f"{CAMPAIGNS}/{bare_campaign}/steps",
        json={"offset_days": 7, "message_angle": "winback_value"},
    )
    assert second.status_code == 201
    assert second.json()["step_no"] == 2
    assert second.json()["campaign_id"] == bare_campaign


def test_post_campaign_step_404s_for_an_unknown_campaign(db: None) -> None:
    response = client.post(
        f"{CAMPAIGNS}/999999999/steps",
        json={"offset_days": 0, "message_angle": "winback_habit"},
    )
    assert response.status_code == 404


@pytest.fixture
def two_due_enrollments(db: None, monkeypatch):
    """A stepless campaign with two due enrollments, and no real agent.

    No steps means the gate skips every enrollment on no_next_step before
    generation is ever reached, so the batch size can be checked without a
    model call; build_default_agent is stubbed so the endpoint does not need
    a configured provider either.
    """
    monkeypatch.setattr(
        "app.api.routers.campaigns.build_default_agent", lambda session, **kwargs: None
    )

    fund_id = 97703
    client_ids = (97730, 97731)
    with SessionLocal() as session:
        row = Campaign(name="batch limit test campaign")
        session.add(row)
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund"))
        session.commit()
        campaign_id = row.campaign_id

        for client_id in client_ids:
            session.add(
                Clients(
                    client_id=client_id,
                    unit_fund_id=fund_id,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                )
            )
        session.commit()
        session.add_all(
            [
                Enrollment(
                    campaign_id=campaign_id, client_id=client_id, is_primary_contact_row=True
                )
                for client_id in client_ids
            ]
        )
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        session.execute(
            delete(TemplateGenerationPlan).where(TemplateGenerationPlan.campaign_id == campaign_id)
        )
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(Clients).where(Clients.client_id.in_(client_ids)))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_generate_attempts_every_due_enrollment_by_default(two_due_enrollments: int) -> None:
    response = client.post(f"{CAMPAIGNS}/{two_due_enrollments}/generate")
    assert response.status_code == 200
    outcomes = response.json()
    assert len(outcomes) == 2
    assert {o["reason"] for o in outcomes} == {"no_next_step"}


def test_generate_limit_caps_the_batch(two_due_enrollments: int) -> None:
    response = client.post(f"{CAMPAIGNS}/{two_due_enrollments}/generate", params={"limit": 1})
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_generate_rejects_a_limit_below_one(two_due_enrollments: int) -> None:
    response = client.post(f"{CAMPAIGNS}/{two_due_enrollments}/generate", params={"limit": 0})
    assert response.status_code == 422


def test_draft_templates_drafts_nothing_when_no_step_makes_anyone_eligible(
    two_due_enrollments: int,
) -> None:
    """Same no-CampaignStep trick as two_due_enrollments, so this proves the
    wiring without needing a configured model provider."""
    response = client.post(f"{CAMPAIGNS}/{two_due_enrollments}/templates/draft")
    assert response.status_code == 200
    body = response.json()
    assert body["estimated_templates"] == 0
    assert body["effective_limit"] is None
    assert body["drafted_count"] == 0
    assert body["skipped_existing"] == 0
    assert body["failed_guardrails"] == 0
    assert body["policy"]["source"] == "default"
    assert body["templates"] == []


def test_draft_templates_404s_for_an_unknown_campaign(db: None) -> None:
    response = client.post(f"{CAMPAIGNS}/9999999/templates/draft")
    assert response.status_code == 404


def test_instantiate_template_404s_for_an_unknown_campaign(db: None) -> None:
    response = client.post(f"{CAMPAIGNS}/9999999/templates/not-a-real-id/instantiate")
    assert response.status_code == 404


def test_instantiate_template_404s_for_a_template_outside_the_campaign(
    two_due_enrollments: int,
) -> None:
    response = client.post(f"{CAMPAIGNS}/{two_due_enrollments}/templates/not-a-real-id/instantiate")
    assert response.status_code == 404
