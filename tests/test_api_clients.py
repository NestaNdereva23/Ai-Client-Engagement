"""The client segment console API: browse buckets, never a name."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text

from app.config import Settings
from app.db.models.llmops import GenerationRun
from app.db.models.models import ClientFeatures, Clients, Funds
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.models.rules import ClientMessageIndicators
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
