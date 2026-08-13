"""The client segment console API: browse buckets, never a name -- except
GET /clients/{id}/name, gated behind the reviewer key stopgap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from app.config import Settings
from app.db.models.audit import AuditLog
from app.db.models.campaigns import CampaignStep, ContactEvent, Enrollment, TouchLog
from app.db.models.llmops import GenerationRun
from app.db.models.models import ClientFeatures, Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.models.rules import ClientMessageIndicators
from app.db.models.suppression import Suppression
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.main import app
from app.services.review import create_outreach_message

client = TestClient(app)

CLIENTS = "/api/v1/clients"
SEGMENTS = "/api/v1/segments"


@pytest.fixture
def roles(db: None):
    with SessionLocal() as session:
        exists = session.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'ace_restricted'"))
    if not exists:
        pytest.skip("boundary roles not present; run alembic upgrade head")


@pytest.fixture
def two_clients(roles):
    """Two clients in one fund, different buckets, one with a resolved angle."""
    fund_id = 974
    first_id, second_id = 97401, 97402
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add_all(
            [
                Clients(
                    client_id=first_id,
                    unit_fund_id=fund_id,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                ),
                Clients(
                    client_id=second_id,
                    unit_fund_id=fund_id,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                ),
            ]
        )
        session.commit()
        session.add_all(
            [
                ClientFeatures(
                    client_id=first_id,
                    purchase_depth="single",
                    recency_band="1 to 3y",
                    value_band="High",
                    cadence_band="Unknown",
                    stale_contact=True,
                ),
                ClientFeatures(
                    client_id=second_id,
                    purchase_depth="few",
                    recency_band="3 to 6y",
                    value_band="Medium",
                    cadence_band="Periodic",
                    stale_contact=False,
                ),
            ]
        )
        session.add(
            ClientMessageIndicators(
                client_id=first_id,
                message_angle="onboarding_retry",
                urgency="high",
                priority_tier="T1",
                prompt_variant="onboarding_retry",
                rule_name="onboarding_retry",
                rule_version=3,
            )
        )
        session.commit()

    yield first_id, second_id, fund_id

    with SessionLocal() as session:
        session.execute(
            delete(ClientMessageIndicators).where(
                ClientMessageIndicators.client_id.in_([first_id, second_id])
            )
        )
        session.execute(
            delete(ClientFeatures).where(ClientFeatures.client_id.in_([first_id, second_id]))
        )
        session.execute(delete(Clients).where(Clients.client_id.in_([first_id, second_id])))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def _make_settings() -> Settings:
    return Settings(
        llm_provider="anthropic",
        anthropic_api_key="test-key",
        llm_model="claude-opus-5",
        llm_temperature=None,
        llm_max_tokens=1024,
    )


@pytest.fixture
def approved_call_brief(two_clients):
    """An approved outreach_message carrying a call_brief for the first client."""
    first_id, _second_id, _fund_id = two_clients
    with SessionLocal() as session:
        campaign = Campaign(name="call brief api test campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id

        run = persist_generation_run(
            session,
            {
                "run_id": str(uuid4()),
                "trace_id": uuid4().hex,
                "client_id": first_id,
                "product": "money market",
                "angle": "onboarding_retry",
                "priority_tier": "T1",
                "prompt_variant": "onboarding_retry",
                "status": "accepted",
                "attempts": 1,
                "failed_guardrail": None,
                "reason": None,
                "raw_structured_output": {
                    "subject": "Come back to {{fund_name}}",
                    "body": "Dear {{first_name}}, we miss you.",
                },
            },
            _make_settings(),
        )
        session.commit()
        message = create_outreach_message(
            session, run, campaign_id=campaign_id, call_brief="Call brief: text"
        )
        message.status = "approved"
        session.commit()
        message_id = message.message_id

    yield first_id

    with SessionLocal() as session:
        session.execute(delete(OutreachMessage).where(OutreachMessage.message_id == message_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run.run_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


@pytest.fixture
def profile_client(two_clients):
    """Adds a campaign, enrollment, touch, outreach_message, contact_event,
    and suppression row for the first of two_clients, so the profile read
    has something in every section to return.
    """
    first_id, _second_id, _fund_id = two_clients
    with SessionLocal() as session:
        campaign = Campaign(name="client profile test campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id
        session.add(
            CampaignStep(
                campaign_id=campaign_id, step_no=1, offset_days=0, message_angle="onboarding_retry"
            )
        )
        session.commit()

        enrollment = Enrollment(
            campaign_id=campaign_id, client_id=first_id, is_primary_contact_row=True
        )
        session.add(enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

        run = persist_generation_run(
            session,
            {
                "run_id": str(uuid4()),
                "trace_id": uuid4().hex,
                "client_id": first_id,
                "product": "money market",
                "angle": "onboarding_retry",
                "priority_tier": "T1",
                "prompt_variant": "onboarding_retry",
                "status": "accepted",
                "attempts": 1,
                "failed_guardrail": None,
                "reason": None,
                "raw_structured_output": {
                    "subject": "Come back to {{fund_name}}",
                    "body": "Dear {{first_name}}, we miss you.",
                },
            },
            _make_settings(),
        )
        session.commit()
        message = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        message_id = message.message_id

        session.add(
            TouchLog(enrollment_id=enrollment_id, step_no=1, message_id=message_id, sent_at=None)
        )
        session.add(ContactEvent(client_id=first_id, type="open", occurred_at=datetime.now(UTC)))
        session.add(Suppression(client_id=first_id, reason="test_suppressed", source="test"))
        session.commit()

    yield first_id, campaign_id

    with SessionLocal() as session:
        session.execute(delete(ContactEvent).where(ContactEvent.client_id == first_id))
        session.execute(delete(Suppression).where(Suppression.client_id == first_id))
        session.execute(delete(TouchLog).where(TouchLog.enrollment_id == enrollment_id))
        session.execute(delete(OutreachMessage).where(OutreachMessage.message_id == message_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run.run_id))
        session.execute(delete(Enrollment).where(Enrollment.enrollment_id == enrollment_id))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def test_get_client_profile_returns_identity_bands_flags_activity_routing(
    two_clients,
) -> None:
    first_id, _second_id, fund_id = two_clients
    response = client.get(f"{CLIENTS}/{first_id}/profile")
    assert response.status_code == 200
    body = response.json()

    assert body["identity"]["client_id"] == first_id
    assert body["identity"]["unit_fund_id"] == fund_id
    assert body["identity"]["fund_name"] == "Cytonn Money Market Fund"
    assert body["bands"]["value_band"] == "High"
    assert body["bands"]["recency_band"] == "1 to 3y"
    assert body["flags"]["stale_contact"] is True
    assert body["routing"]["message_angle"] == "onboarding_retry"
    assert body["routing"]["rule_name"] == "onboarding_retry"
    assert body["routing"]["rule_version"] == 3


def test_get_client_profile_never_includes_a_name(two_clients) -> None:
    first_id, _second_id, _fund_id = two_clients
    response = client.get(f"{CLIENTS}/{first_id}/profile")
    body = response.json()
    assert "client_name" not in body["identity"]
    assert "name" not in body["identity"]


def test_get_client_profile_404s_for_an_unknown_client(db: None) -> None:
    response = client.get(f"{CLIENTS}/9999999/profile")
    assert response.status_code == 404


def test_get_client_profile_empty_history_is_empty_lists_not_missing_keys(two_clients) -> None:
    first_id, _second_id, _fund_id = two_clients
    response = client.get(f"{CLIENTS}/{first_id}/profile")
    body = response.json()
    assert body["enrollments"] == []
    assert body["touch_log"] == []
    assert body["outreach_messages"] == []
    assert body["contact_events"] == []
    assert body["suppression"] == {
        "is_suppressed": False,
        "reason": None,
        "source": None,
        "created_at": None,
    }


def test_get_client_profile_includes_campaign_and_engagement_history(profile_client) -> None:
    first_id, campaign_id = profile_client
    response = client.get(f"{CLIENTS}/{first_id}/profile")
    body = response.json()

    assert len(body["enrollments"]) == 1
    assert body["enrollments"][0]["campaign_id"] == campaign_id
    assert body["enrollments"][0]["status"] == "enrolled"

    assert len(body["touch_log"]) == 1
    assert body["touch_log"][0]["step_no"] == 1

    assert len(body["outreach_messages"]) == 1
    message = body["outreach_messages"][0]
    assert message["campaign_id"] == campaign_id
    assert message["status"] == "pending_review"
    assert "ai_draft_content" not in message
    assert "personalized_content" not in message

    assert len(body["contact_events"]) == 1
    assert body["contact_events"][0]["type"] == "open"


def test_get_client_profile_reports_suppression_reason(profile_client) -> None:
    first_id, _campaign_id = profile_client
    response = client.get(f"{CLIENTS}/{first_id}/profile")
    suppression = response.json()["suppression"]
    assert suppression["is_suppressed"] is True
    assert suppression["reason"] == "test_suppressed"


def test_get_client_profile_returns_the_latest_approved_call_brief(approved_call_brief) -> None:
    first_id = approved_call_brief
    response = client.get(f"{CLIENTS}/{first_id}/profile")
    assert response.status_code == 200
    assert response.json()["call_brief"] == "Call brief: text"


def test_get_client_detail_returns_the_latest_approved_call_brief(approved_call_brief) -> None:
    first_id = approved_call_brief
    response = client.get(f"{CLIENTS}/{first_id}")
    assert response.status_code == 200
    assert response.json()["call_brief"] == "Call brief: text"


def test_get_client_detail_call_brief_is_null_with_no_approved_message(two_clients) -> None:
    first_id, _second_id, _fund_id = two_clients
    response = client.get(f"{CLIENTS}/{first_id}")
    assert response.status_code == 200
    assert response.json()["call_brief"] is None


def test_list_clients_never_includes_a_call_brief(approved_call_brief) -> None:
    first_id = approved_call_brief
    response = client.get(CLIENTS, params={"client_id": first_id})
    items = response.json()["items"]
    assert items
    for row in items:
        assert row.get("call_brief") is None


def test_list_clients_returns_buckets_and_never_a_name(two_clients) -> None:
    first_id, _second_id, fund_id = two_clients
    response = client.get(CLIENTS, params={"fund_id": fund_id})
    assert response.status_code == 200
    items = response.json()["items"]
    ids = [row["client_id"] for row in items]
    assert first_id in ids
    for row in items:
        assert "client_name" not in row
        assert "name" not in row


def test_list_clients_filters_by_client_id(two_clients) -> None:
    first_id, second_id, fund_id = two_clients
    response = client.get(CLIENTS, params={"fund_id": fund_id, "client_id": first_id})
    ids = [row["client_id"] for row in response.json()["items"]]
    assert ids == [first_id]
    assert second_id not in ids


def test_get_client_detail_returns_the_same_bucket_shape(two_clients) -> None:
    first_id, _second_id, _fund_id = two_clients
    response = client.get(f"{CLIENTS}/{first_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["client_id"] == first_id
    assert body["value_band"] == "High"
    assert body["message_angle"] == "onboarding_retry"
    assert "client_name" not in body
    assert "name" not in body


def test_get_client_detail_404s_for_an_unknown_client(db: None) -> None:
    response = client.get(f"{CLIENTS}/9999999")
    assert response.status_code == 404


def test_list_clients_filters_by_purchase_depth(two_clients) -> None:
    first_id, second_id, fund_id = two_clients
    response = client.get(CLIENTS, params={"fund_id": fund_id, "purchase_depth": "single"})
    ids = [row["client_id"] for row in response.json()["items"]]
    assert first_id in ids
    assert second_id not in ids


def test_list_clients_filters_by_message_angle(two_clients) -> None:
    first_id, second_id, fund_id = two_clients
    response = client.get(CLIENTS, params={"fund_id": fund_id, "message_angle": "onboarding_retry"})
    ids = [row["client_id"] for row in response.json()["items"]]
    assert first_id in ids
    assert second_id not in ids


def test_list_clients_filters_by_newly_dormant(two_clients) -> None:
    first_id, second_id, fund_id = two_clients
    with SessionLocal() as session:
        row = session.get(ClientFeatures, first_id)
        row.newly_dormant = True
        session.commit()

    response = client.get(CLIENTS, params={"fund_id": fund_id, "newly_dormant": True})
    ids = [row["client_id"] for row in response.json()["items"]]
    assert first_id in ids
    assert second_id not in ids


def test_segments_counts_include_the_new_buckets(two_clients) -> None:
    response = client.get(SEGMENTS)
    assert response.status_code == 200
    body = response.json()
    depths = {row["key"]: row["count"] for row in body["by_purchase_depth"]}
    assert depths.get("single", 0) >= 1
    assert depths.get("few", 0) >= 1


def test_segments_stale_contact_count_reflects_the_stale_client(two_clients) -> None:
    first_id, _second_id, _fund_id = two_clients
    before = client.get(SEGMENTS).json()["stale_contact_count"]

    with SessionLocal() as session:
        row = session.get(ClientFeatures, first_id)
        row.stale_contact = False
        session.commit()

    after = client.get(SEGMENTS).json()["stale_contact_count"]
    assert after == before - 1


# --- GET /clients/{id}/name: the gated name re-attachment ------------------


def test_client_name_missing_header_is_401(configured_reviewers, two_clients) -> None:
    first_id, _second_id, _fund_id = two_clients
    response = client.get(f"{CLIENTS}/{first_id}/name")
    assert response.status_code == 401


def test_client_name_wrong_key_is_401(configured_reviewers, two_clients) -> None:
    first_id, _second_id, _fund_id = two_clients
    response = client.get(f"{CLIENTS}/{first_id}/name", headers={"X-Reviewer-Key": "wrong"})
    assert response.status_code == 401


def test_client_name_no_key_configured_is_503(
    unconfigured_reviewers, two_clients, reviewer_1_headers
) -> None:
    first_id, _second_id, _fund_id = two_clients
    response = client.get(f"{CLIENTS}/{first_id}/name", headers=reviewer_1_headers)
    assert response.status_code == 503


def test_client_name_404s_for_an_unknown_client(
    configured_reviewers, db: None, reviewer_1_headers
) -> None:
    response = client.get(f"{CLIENTS}/9999999/name", headers=reviewer_1_headers)
    assert response.status_code == 404


def test_client_name_is_null_with_no_pii_vault_row(
    configured_reviewers, two_clients, reviewer_1_headers
) -> None:
    """A real, common state -- not a 404 -- for a client whose contact
    channels have not synced in yet."""
    first_id, _second_id, _fund_id = two_clients
    response = client.get(f"{CLIENTS}/{first_id}/name", headers=reviewer_1_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["client_id"] == first_id
    assert body["client_name"] is None


@pytest.fixture
def named_client(two_clients):
    first_id, _second_id, _fund_id = two_clients
    with SessionLocal() as session:
        session.add(PiiVault(client_id=first_id, client_name="Jane Doe"))
        session.commit()

    yield first_id

    with SessionLocal() as session:
        session.execute(delete(PiiVault).where(PiiVault.client_id == first_id))
        session.commit()


def test_client_name_returns_the_real_name_with_a_valid_key(
    configured_reviewers, named_client, reviewer_1_headers
) -> None:
    response = client.get(f"{CLIENTS}/{named_client}/name", headers=reviewer_1_headers)
    assert response.status_code == 200
    assert response.json()["client_name"] == "Jane Doe"


def test_client_name_read_is_audited(
    configured_reviewers, named_client, reviewer_1_headers
) -> None:
    response = client.get(f"{CLIENTS}/{named_client}/name", headers=reviewer_1_headers)
    assert response.status_code == 200

    with SessionLocal() as session:
        rows = session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "pii_vault",
                AuditLog.entity_id == str(named_client),
                AuditLog.action == "read",
            )
            .order_by(AuditLog.log_id.desc())
        ).all()
    assert rows, "expected a pii_vault audit row for this read"
    assert rows[0].actor_id == "fa-1"


def test_get_client_profile_still_needs_no_key_and_never_includes_a_name(
    named_client,
) -> None:
    """The profile endpoint (item 13) must stay exactly as it was: no
    auth, no name, even for a client the name endpoint can now identify.
    """
    response = client.get(f"{CLIENTS}/{named_client}/profile")
    assert response.status_code == 200
    assert "Jane Doe" not in response.text
    assert "client_name" not in response.json()["identity"]
