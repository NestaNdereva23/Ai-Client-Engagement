"""The template review queue HTTP API: list pending, open one, and decide."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import Settings
from app.db.models.llmops import GenerationRun
from app.db.models.message_template import MessageTemplate, TemplateReviewAction
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.main import app

client = TestClient(app)

TEMPLATES = "/api/v1/templates"

PROFILE_KEY = {
    "message_angle": "pick_up_again",
    "priority_tier": "T3",
    "product": "money market",
    "has_cadence": True,
    "stale_contact": False,
    "exit_reason_charge_settled": False,
    "fund_name_known": False,
}


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
        "priority_tier": "T3",
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


@pytest.fixture
def template(db: None):
    """A pending_review message_template backed by a real client, fund, and run."""
    fund_id = 975
    client_id = 97501
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
        run = persist_generation_run(session, accepted_state(client_id), make_settings())
        campaign = Campaign(name="api template test campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id
        row = MessageTemplate(
            template_id=uuid4().hex,
            campaign_id=campaign_id,
            generation_run_id=run.run_id,
            profile_key=PROFILE_KEY,
            ai_draft_content={
                "subject": "Come back to {{fund_name}}",
                "body": "Dear {{first_name}}, we miss you.",
            },
        )
        session.add(row)
        session.commit()
        template_id = row.template_id
        run_id = run.run_id

    yield template_id, campaign_id

    with SessionLocal() as session:
        session.execute(
            delete(TemplateReviewAction).where(TemplateReviewAction.template_id == template_id)
        )
        session.execute(delete(MessageTemplate).where(MessageTemplate.template_id == template_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_list_templates_returns_the_pending_template(template) -> None:
    template_id, _campaign_id = template
    response = client.get(TEMPLATES)
    assert response.status_code == 200
    ids = [row["template_id"] for row in response.json()["items"]]
    assert template_id in ids


def test_list_templates_filters_by_campaign(template) -> None:
    template_id, campaign_id = template
    matched = client.get(TEMPLATES, params={"campaign_id": campaign_id})
    unmatched = client.get(TEMPLATES, params={"campaign_id": campaign_id + 1})
    assert template_id in [row["template_id"] for row in matched.json()["items"]]
    assert template_id not in [row["template_id"] for row in unmatched.json()["items"]]


def test_get_template_detail_returns_the_draft_and_profile(template) -> None:
    template_id, _campaign_id = template
    response = client.get(f"{TEMPLATES}/{template_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["ai_draft_content"]["body"] == "Dear {{first_name}}, we miss you."
    assert body["profile_key"] == PROFILE_KEY
    assert body["history"] == []


def test_get_template_detail_404s_when_not_found(db: None) -> None:
    response = client.get(f"{TEMPLATES}/not-a-real-id")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_decide_with_no_reviewer_key_is_401(configured_reviewers, template) -> None:
    template_id, _campaign_id = template
    response = client.post(f"{TEMPLATES}/{template_id}/decide", json={"outcome": "approve"})
    assert response.status_code == 401


def test_decide_with_no_reviewers_configured_is_503(
    unconfigured_reviewers, template, reviewer_1_headers
) -> None:
    template_id, _campaign_id = template
    response = client.post(
        f"{TEMPLATES}/{template_id}/decide",
        json={"outcome": "approve"},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 503


def test_decide_approve_returns_the_action_and_updates_status(
    configured_reviewers, template, reviewer_1_headers
) -> None:
    template_id, _campaign_id = template
    response = client.post(
        f"{TEMPLATES}/{template_id}/decide",
        json={"outcome": "approve"},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "approve"
    assert body["message_angle"] == "pick_up_again"
    assert body["priority_tier"] == "T3"
    assert body["reviewer_id"] == "fa-1"

    detail = client.get(f"{TEMPLATES}/{template_id}").json()
    assert detail["status"] == "approved"
    assert len(detail["history"]) == 1


def test_decide_edit_approve_stores_the_edit_and_updates_the_draft(
    configured_reviewers, template, reviewer_1_headers
) -> None:
    template_id, _campaign_id = template
    edited = {"subject": "New subject", "body": "New body, {{first_name}}."}
    response = client.post(
        f"{TEMPLATES}/{template_id}/decide",
        json={"outcome": "edit_approve", "edited_content": edited},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 200
    assert response.json()["edited_content"] == edited

    detail = client.get(f"{TEMPLATES}/{template_id}").json()
    assert detail["ai_draft_content"] == edited


def test_decide_edit_approve_without_content_is_rejected(
    configured_reviewers, template, reviewer_1_headers
) -> None:
    template_id, _campaign_id = template
    response = client.post(
        f"{TEMPLATES}/{template_id}/decide",
        json={"outcome": "edit_approve"},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_decide_twice_is_a_conflict(
    configured_reviewers, template, reviewer_1_headers, reviewer_2_headers
) -> None:
    template_id, _campaign_id = template
    first = client.post(
        f"{TEMPLATES}/{template_id}/decide",
        json={"outcome": "approve"},
        headers=reviewer_1_headers,
    )
    assert first.status_code == 200

    second = client.post(
        f"{TEMPLATES}/{template_id}/decide",
        json={"outcome": "reject"},
        headers=reviewer_2_headers,
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


def test_decide_404s_when_the_template_does_not_exist(
    configured_reviewers, db: None, reviewer_1_headers
) -> None:
    response = client.post(
        f"{TEMPLATES}/not-a-real-id/decide",
        json={"outcome": "approve"},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 404
