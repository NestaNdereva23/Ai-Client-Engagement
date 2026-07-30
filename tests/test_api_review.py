"""The review queue HTTP API: list pending, open one, and decide.

Drives the router the same way a real client would, through TestClient
against the real app, so these prove the wiring (routing, request/response
shapes, status codes) rather than re-testing the service logic already
covered in test_services_review.py.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text

from app.config import Settings
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.main import app
from app.services.review import create_outreach_message

client = TestClient(app)


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
        "angle": "winback_habit",
        "prompt_variant": "habit_premium",
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}, we miss you.",
        },
    }


@pytest.fixture
def roles(db: None):
    with SessionLocal() as session:
        exists = session.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'ace_restricted'"))
    if not exists:
        pytest.skip("boundary roles not present; run alembic upgrade head")


@pytest.fixture
def message(roles):
    """A pending_review outreach_message backed by a real client, fund, and run."""
    fund_id = 971
    client_id = 97101
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.add(PiiVault(client_id=client_id, client_name="Jane Doe"))
        session.commit()
        run = persist_generation_run(session, accepted_state(client_id), make_settings())
        campaign = Campaign(name="api test campaign")
        session.add(campaign)
        session.commit()
        created = create_outreach_message(session, run, campaign_id=campaign.campaign_id)
        session.commit()
        message_id, run_id, campaign_id_val = created.message_id, run.run_id, campaign.campaign_id

    yield message_id, campaign_id_val

    with SessionLocal() as session:
        session.execute(delete(ReviewAction).where(ReviewAction.message_id == message_id))
        session.execute(delete(OutreachMessage).where(OutreachMessage.message_id == message_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id_val))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_list_reviews_returns_the_pending_message(message) -> None:
    message_id, _campaign_id = message
    response = client.get("/reviews")
    assert response.status_code == 200
    ids = [row["message_id"] for row in response.json()]
    assert message_id in ids


def test_list_reviews_filters_by_campaign(message) -> None:
    message_id, campaign_id = message
    matched = client.get("/reviews", params={"campaign_id": campaign_id})
    unmatched = client.get("/reviews", params={"campaign_id": campaign_id + 1})
    assert message_id in [row["message_id"] for row in matched.json()]
    assert message_id not in [row["message_id"] for row in unmatched.json()]


def test_get_review_returns_both_content_versions(message) -> None:
    message_id, _campaign_id = message
    response = client.get(f"/reviews/{message_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["ai_draft_content"]["body"] == "Dear {{first_name}}, we miss you."
    assert body["personalized_content"]["body"] == "Dear Jane, we miss you."
    assert body["history"] == []


def test_get_review_404s_when_not_found(db: None) -> None:
    response = client.get("/reviews/not-a-real-id")
    assert response.status_code == 404


def test_decide_approve_returns_the_action_and_updates_status(message) -> None:
    message_id, _campaign_id = message
    response = client.post(
        f"/reviews/{message_id}/decide",
        json={"outcome": "approve", "reviewer_id": "fa-1"},
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "approve"

    detail = client.get(f"/reviews/{message_id}").json()
    assert detail["status"] == "approved"
    assert len(detail["history"]) == 1


def test_decide_edit_approve_without_content_is_rejected(message) -> None:
    message_id, _campaign_id = message
    response = client.post(
        f"/reviews/{message_id}/decide",
        json={"outcome": "edit_approve", "reviewer_id": "fa-1"},
    )
    assert response.status_code == 422


def test_decide_edit_approve_stores_the_edit(message) -> None:
    message_id, _campaign_id = message
    edited = {"subject": "New subject", "body": "New body"}
    response = client.post(
        f"/reviews/{message_id}/decide",
        json={"outcome": "edit_approve", "reviewer_id": "fa-1", "edited_content": edited},
    )
    assert response.status_code == 200
    assert response.json()["edited_content"] == edited


def test_decide_twice_is_a_conflict(message) -> None:
    message_id, _campaign_id = message
    first = client.post(
        f"/reviews/{message_id}/decide",
        json={"outcome": "approve", "reviewer_id": "fa-1"},
    )
    assert first.status_code == 200

    second = client.post(
        f"/reviews/{message_id}/decide",
        json={"outcome": "reject", "reviewer_id": "fa-2"},
    )
    assert second.status_code == 409


def test_decide_404s_when_the_message_does_not_exist(db: None) -> None:
    response = client.post(
        "/reviews/not-a-real-id/decide",
        json={"outcome": "approve", "reviewer_id": "fa-1"},
    )
    assert response.status_code == 404
