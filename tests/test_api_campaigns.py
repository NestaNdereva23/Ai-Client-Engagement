"""The campaign console API: enrollment totals, including primary-row suppression."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import Settings
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog
from app.db.models.llmops import GenerationRun
from app.db.models.message_template import MessageTemplate
from app.db.models.models import ClientFeatures, Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.models.template_generation_plan import TemplateGenerationPlan
from app.db.session import SessionLocal, restricted_session
from app.llmops.versions import persist_generation_run
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


@pytest.fixture
def campaign_with_valued_clients(db: None):
    """One campaign, one suppressed duplicate, so the value sum can be
    checked against only the primary row's purchase amount.
    """
    fund_id = 97702
    primary_id, suppressed_id = 97720, 97721
    with SessionLocal() as session:
        row = Campaign(name="test value campaign")
        session.add(row)
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund"))
        session.commit()
        campaign_id = row.campaign_id

        session.add(
            Clients(
                client_id=primary_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
                total_purchase_amount=125_000.0,
            )
        )
        session.add(
            Clients(
                client_id=suppressed_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
                total_purchase_amount=99_000.0,
            )
        )
        session.add_all(
            [
                PiiVault(client_id=primary_id, client_name="Valued Person"),
                PiiVault(client_id=suppressed_id, client_name="Valued Person"),
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


def test_campaign_value_sums_primary_rows_only(campaign_with_valued_clients) -> None:
    campaign_id = campaign_with_valued_clients
    response = client.get(f"{CAMPAIGNS}/{campaign_id}/value")
    assert response.status_code == 200
    body = response.json()
    assert body["campaign_id"] == campaign_id
    assert body["valued_count"] == 1
    assert body["estimated_value"] == 125_000.0


def test_campaign_value_404s_for_an_unknown_campaign(db: None) -> None:
    response = client.get(f"{CAMPAIGNS}/999999999/value")
    assert response.status_code == 404


def make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "llm_model": "claude-opus-5",
        "llm_temperature": None,
        "llm_max_tokens": 1024,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def accepted_state(client_id: int) -> dict:
    return {
        "run_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "client_id": client_id,
        "product": "money market",
        "angle": "pick_up_again",
        "prompt_variant": "pick_up_again",
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}, we miss you.",
        },
    }


PROFILE_KEY = {
    "message_angle": "pick_up_again",
    "priority_tier": "T3",
    "product": "money market",
    "has_cadence": True,
    "stale_contact": False,
    "exit_reason_charge_settled": False,
    "fund_name_known": False,
}


@pytest.fixture
def campaign_with_a_template_and_a_message(db: None):
    """One campaign: one approved template, one pending_review message."""
    fund_id = 97702
    client_id = 97720
    with SessionLocal() as session:
        campaign = Campaign(name="test readiness campaign")
        session.add(campaign)
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund"))
        session.commit()
        campaign_id = campaign.campaign_id

        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.commit()

        template_run = persist_generation_run(session, accepted_state(client_id), make_settings())
        template = MessageTemplate(
            template_id=uuid4().hex,
            campaign_id=campaign_id,
            generation_run_id=template_run.run_id,
            profile_key=PROFILE_KEY,
            ai_draft_content={"subject": "Subject", "body": "Body"},
            status="approved",
        )
        session.add(template)

        message_run = persist_generation_run(session, accepted_state(client_id), make_settings())
        message = OutreachMessage(
            message_id=uuid4().hex,
            campaign_id=campaign_id,
            generation_run_id=message_run.run_id,
            client_id=client_id,
            ai_draft_content={"subject": "Subject", "body": "Body"},
        )
        session.add(message)
        session.commit()
        run_ids = [template_run.run_id, message_run.run_id]

    yield campaign_id

    with SessionLocal() as session:
        session.execute(delete(OutreachMessage).where(OutreachMessage.campaign_id == campaign_id))
        session.execute(delete(MessageTemplate).where(MessageTemplate.campaign_id == campaign_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id.in_(run_ids)))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_campaign_readiness_returns_per_status_counts(
    campaign_with_a_template_and_a_message,
) -> None:
    campaign_id = campaign_with_a_template_and_a_message
    response = client.get(f"{CAMPAIGNS}/{campaign_id}/readiness")
    assert response.status_code == 200
    body = response.json()
    assert body["campaign_id"] == campaign_id
    assert body["templates"] == {"approved": 1}
    assert body["messages"] == {"pending_review": 1}


def test_campaign_readiness_404s_for_an_unknown_campaign(db: None) -> None:
    response = client.get(f"{CAMPAIGNS}/999999999/readiness")
    assert response.status_code == 404


def test_get_campaign_detail_returns_the_campaign_row(
    campaign_with_a_suppressed_row,
) -> None:
    campaign_id = campaign_with_a_suppressed_row
    response = client.get(f"{CAMPAIGNS}/{campaign_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["campaign_id"] == campaign_id
    assert body["name"] == "test summary campaign"
    assert body["campaign_type"] == "dormant_reengagement"
    assert body["status"] == "draft"
    assert "total_enrolled" not in body


def test_get_campaign_detail_404s_for_an_unknown_campaign(db: None) -> None:
    response = client.get(f"{CAMPAIGNS}/999999999")
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


def test_get_campaign_enrollments_returns_the_roster(
    campaign_with_a_suppressed_row,
) -> None:
    campaign_id = campaign_with_a_suppressed_row
    response = client.get(f"{CAMPAIGNS}/{campaign_id}/enrollments")
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    client_ids = {row["client_id"] for row in body["items"]}
    assert client_ids == {97710, 97711}
    statuses = {row["status"] for row in body["items"]}
    assert statuses == {"enrolled"}


def test_get_campaign_enrollments_paginates(campaign_with_a_suppressed_row) -> None:
    campaign_id = campaign_with_a_suppressed_row
    first = client.get(f"{CAMPAIGNS}/{campaign_id}/enrollments", params={"limit": 1})
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["items"]) == 1
    assert first_body["next_cursor"] is not None

    second = client.get(
        f"{CAMPAIGNS}/{campaign_id}/enrollments",
        params={"limit": 1, "cursor": first_body["next_cursor"]},
    )
    assert second.status_code == 200
    second_body = second.json()
    assert len(second_body["items"]) == 1
    assert second_body["items"][0]["client_id"] != first_body["items"][0]["client_id"]
    assert second_body["next_cursor"] is None


def test_get_campaign_enrollments_404s_for_an_unknown_campaign(db: None) -> None:
    response = client.get(f"{CAMPAIGNS}/999999999/enrollments")
    assert response.status_code == 404


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


def test_post_campaign_step_422s_for_an_offset_equal_to_the_previous_step(
    bare_campaign: int,
) -> None:
    first = client.post(
        f"{CAMPAIGNS}/{bare_campaign}/steps",
        json={"offset_days": 0, "message_angle": "winback_habit"},
    )
    assert first.status_code == 201

    second = client.post(
        f"{CAMPAIGNS}/{bare_campaign}/steps",
        json={"offset_days": 0, "message_angle": "winback_value"},
    )
    assert second.status_code == 422

    # rejected: it did not get appended
    steps = client.get(f"{CAMPAIGNS}/{bare_campaign}/steps").json()
    assert [s["step_no"] for s in steps] == [1]


def test_post_campaign_step_422s_for_an_offset_smaller_than_the_previous_step(
    bare_campaign: int,
) -> None:
    first = client.post(
        f"{CAMPAIGNS}/{bare_campaign}/steps",
        json={"offset_days": 7, "message_angle": "winback_habit"},
    )
    assert first.status_code == 201

    second = client.post(
        f"{CAMPAIGNS}/{bare_campaign}/steps",
        json={"offset_days": 3, "message_angle": "winback_value"},
    )
    assert second.status_code == 422


def test_post_campaign_step_404s_for_an_unknown_campaign(db: None) -> None:
    response = client.post(
        f"{CAMPAIGNS}/999999999/steps",
        json={"offset_days": 0, "message_angle": "winback_habit"},
    )
    assert response.status_code == 404


def test_get_campaign_steps_returns_the_full_persisted_sequence(bare_campaign: int) -> None:
    client.post(
        f"{CAMPAIGNS}/{bare_campaign}/steps",
        json={"offset_days": 0, "message_angle": "winback_habit"},
    )
    client.post(
        f"{CAMPAIGNS}/{bare_campaign}/steps",
        json={"offset_days": 7, "message_angle": "winback_value"},
    )

    response = client.get(f"{CAMPAIGNS}/{bare_campaign}/steps")

    assert response.status_code == 200
    steps = response.json()
    assert [s["step_no"] for s in steps] == [1, 2]
    assert [s["message_angle"] for s in steps] == ["winback_habit", "winback_value"]


def test_get_campaign_steps_is_empty_for_a_stepless_campaign(bare_campaign: int) -> None:
    response = client.get(f"{CAMPAIGNS}/{bare_campaign}/steps")

    assert response.status_code == 200
    assert response.json() == []


def test_get_campaign_steps_404s_for_an_unknown_campaign(db: None) -> None:
    response = client.get(f"{CAMPAIGNS}/999999999/steps")
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


@pytest.fixture
def campaign_with_an_approved_touch(db: None):
    """One draft campaign, one enrollment, one touch already generated and approved.

    Ready for POST .../send to pick up: the touch has sent_at unset, its
    message is approved, so send_due_touches finds exactly this one row.
    """
    fund_id = 97704
    client_id = 97740
    with SessionLocal() as session:
        campaign = Campaign(name="test send campaign")
        session.add(campaign)
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund"))
        session.commit()
        campaign_id = campaign.campaign_id
        assert campaign.status == "draft"

        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.commit()

    with restricted_session() as session:
        session.add(
            PiiVault(
                client_id=client_id, client_name="Test Client", contact_email="test@example.com"
            )
        )
        session.commit()

    with SessionLocal() as session:
        enrollment = Enrollment(campaign_id=campaign_id, client_id=client_id)
        session.add(enrollment)
        session.commit()

        message_run = persist_generation_run(session, accepted_state(client_id), make_settings())
        message = OutreachMessage(
            message_id=uuid4().hex,
            campaign_id=campaign_id,
            generation_run_id=message_run.run_id,
            client_id=client_id,
            ai_draft_content={"subject": "Subject", "body": "Body"},
            status="approved",
        )
        session.add(message)
        session.commit()

        enrollment_id = enrollment.enrollment_id
        message_run_id = message_run.run_id

        touch = TouchLog(enrollment_id=enrollment_id, step_no=1, message_id=message.message_id)
        session.add(touch)
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        session.execute(delete(TouchLog).where(TouchLog.enrollment_id == enrollment_id))
        session.execute(delete(OutreachMessage).where(OutreachMessage.campaign_id == campaign_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == message_run_id))
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()

    with restricted_session() as session:
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.commit()


def test_send_sends_the_approved_touch_and_flips_the_campaign_to_running(
    campaign_with_an_approved_touch: int,
) -> None:
    response = client.post(f"{CAMPAIGNS}/{campaign_with_an_approved_touch}/send")
    assert response.status_code == 200
    outcomes = response.json()
    assert len(outcomes) == 1
    assert outcomes[0]["sent"] is True
    assert outcomes[0]["delivery_status"] == "stubbed"

    detail = client.get(f"{CAMPAIGNS}/{campaign_with_an_approved_touch}")
    assert detail.json()["status"] == "running"


def test_send_is_a_no_op_the_second_time(campaign_with_an_approved_touch: int) -> None:
    client.post(f"{CAMPAIGNS}/{campaign_with_an_approved_touch}/send")
    response = client.post(f"{CAMPAIGNS}/{campaign_with_an_approved_touch}/send")
    assert response.status_code == 200
    assert response.json() == []


def test_send_404s_for_an_unknown_campaign(db: None) -> None:
    response = client.post(f"{CAMPAIGNS}/9999999/send")
    assert response.status_code == 404
