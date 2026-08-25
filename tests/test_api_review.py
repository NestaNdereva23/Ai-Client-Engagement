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
from app.db.models.api import IdempotencyKey
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.main import app
from app.services.review import create_outreach_message

client = TestClient(app)

REVIEWS = "/api/v1/reviews"


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


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


@pytest.fixture
def two_messages(roles):
    """Two pending_review outreach_messages, same campaign, for paging through."""
    fund_id = 973
    client_id = 97301
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
        campaign = Campaign(name="api pagination test campaign")
        session.add(campaign)
        session.commit()
        campaign_id_val = campaign.campaign_id

        run_ids = []
        message_ids = []
        for _ in range(2):
            run = persist_generation_run(session, accepted_state(client_id), make_settings())
            created = create_outreach_message(session, run, campaign_id=campaign_id_val)
            session.commit()
            run_ids.append(run.run_id)
            message_ids.append(created.message_id)

    yield message_ids, campaign_id_val

    with SessionLocal() as session:
        session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
        session.execute(delete(OutreachMessage).where(OutreachMessage.message_id.in_(message_ids)))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id.in_(run_ids)))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id_val))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_list_reviews_returns_the_pending_message(message) -> None:
    message_id, _campaign_id = message
    response = client.get(REVIEWS)
    assert response.status_code == 200
    ids = [row["message_id"] for row in response.json()["items"]]
    assert message_id in ids


def test_list_reviews_filters_by_campaign(message) -> None:
    message_id, campaign_id = message
    matched = client.get(REVIEWS, params={"campaign_id": campaign_id})
    unmatched = client.get(REVIEWS, params={"campaign_id": campaign_id + 1})
    assert message_id in [row["message_id"] for row in matched.json()["items"]]
    assert message_id not in [row["message_id"] for row in unmatched.json()["items"]]


def test_get_review_returns_both_content_versions(message) -> None:
    message_id, _campaign_id = message
    response = client.get(f"{REVIEWS}/{message_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["ai_draft_content"]["body"] == "Dear {{first_name}}, we miss you."
    assert body["personalized_content"]["body"] == "Dear Jane, we miss you."
    assert body["history"] == []


def test_get_review_404s_when_not_found(db: None) -> None:
    response = client.get(f"{REVIEWS}/not-a-real-id")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_decide_with_no_reviewer_key_is_401(configured_reviewers, message) -> None:
    message_id, _campaign_id = message
    response = TestClient(app).post(f"{REVIEWS}/{message_id}/decide", json={"outcome": "approve"})
    assert response.status_code == 401


def test_decide_with_no_reviewers_configured_is_503(
    unconfigured_reviewers, message, reviewer_1_headers
) -> None:
    message_id, _campaign_id = message
    response = client.post(
        f"{REVIEWS}/{message_id}/decide", json={"outcome": "approve"}, headers=reviewer_1_headers
    )
    assert response.status_code == 503


def test_decide_approve_returns_the_action_and_updates_status(
    configured_reviewers, message, reviewer_1_headers
) -> None:
    message_id, _campaign_id = message
    response = client.post(
        f"{REVIEWS}/{message_id}/decide",
        json={"outcome": "approve"},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 200
    body = response.json()["action"]
    assert body["outcome"] == "approve"
    assert body["reviewer_id"] == "fa-1"

    detail = client.get(f"{REVIEWS}/{message_id}").json()
    assert detail["status"] == "approved"
    assert len(detail["history"]) == 1


def test_decide_edit_approve_without_content_is_rejected(
    configured_reviewers, message, reviewer_1_headers
) -> None:
    message_id, _campaign_id = message
    response = client.post(
        f"{REVIEWS}/{message_id}/decide",
        json={"outcome": "edit_approve"},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_decide_edit_approve_stores_the_edit(
    configured_reviewers, message, reviewer_1_headers
) -> None:
    message_id, _campaign_id = message
    edited = {"subject": "New subject", "body": "New body"}
    response = client.post(
        f"{REVIEWS}/{message_id}/decide",
        json={"outcome": "edit_approve", "edited_content": edited},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 200
    assert response.json()["action"]["edited_content"] == edited


def test_decide_twice_is_a_conflict(
    configured_reviewers, message, reviewer_1_headers, reviewer_2_headers
) -> None:
    message_id, _campaign_id = message
    first = client.post(
        f"{REVIEWS}/{message_id}/decide",
        json={"outcome": "approve"},
        headers=reviewer_1_headers,
    )
    assert first.status_code == 200

    second = client.post(
        f"{REVIEWS}/{message_id}/decide",
        json={"outcome": "reject"},
        headers=reviewer_2_headers,
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


def test_decide_404s_when_the_message_does_not_exist(
    configured_reviewers, db: None, reviewer_1_headers
) -> None:
    response = client.post(
        f"{REVIEWS}/not-a-real-id/decide",
        json={"outcome": "approve"},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 404


def test_decide_replays_the_first_result_for_a_repeated_idempotency_key(
    configured_reviewers, message, reviewer_1_headers, reviewer_2_headers
) -> None:
    message_id, _campaign_id = message
    key = str(uuid4())
    headers = {"Idempotency-Key": key, **reviewer_1_headers}
    replay_headers = {"Idempotency-Key": key, **reviewer_2_headers}
    try:
        first = client.post(
            f"{REVIEWS}/{message_id}/decide",
            json={"outcome": "approve"},
            headers=headers,
        )
        assert first.status_code == 200

        replay = client.post(
            f"{REVIEWS}/{message_id}/decide",
            json={"outcome": "reject"},
            headers=replay_headers,
        )
        assert replay.status_code == 200
        assert replay.json() == first.json()

        detail = client.get(f"{REVIEWS}/{message_id}").json()
        assert detail["status"] == "approved"
        assert len(detail["history"]) == 1
    finally:
        with SessionLocal() as session:
            session.execute(delete(IdempotencyKey).where(IdempotencyKey.idempotency_key == key))
            session.commit()


def test_decide_batch_approves_every_message(
    configured_reviewers, two_messages, reviewer_1_headers
) -> None:
    message_ids, _campaign_id = two_messages
    response = client.post(
        f"{REVIEWS}/decide-batch",
        json={"message_ids": message_ids, "outcome": "approve"},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert [a["outcome"] for a in body["decided"]] == ["approve", "approve"]
    assert body["failed"] == []

    for message_id in message_ids:
        detail = client.get(f"{REVIEWS}/{message_id}").json()
        assert detail["status"] == "approved"


def test_decide_batch_reports_a_missing_message_without_failing_the_rest(
    configured_reviewers, two_messages, reviewer_1_headers
) -> None:
    message_ids, _campaign_id = two_messages
    response = client.post(
        f"{REVIEWS}/decide-batch",
        json={"message_ids": [message_ids[0], "not-a-real-id"], "outcome": "approve"},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["decided"]) == 1
    assert body["failed"] == [{"message_id": "not-a-real-id", "error": "not_found"}]


def test_decide_batch_rejects_edit_approve(configured_reviewers, reviewer_1_headers) -> None:
    response = client.post(
        f"{REVIEWS}/decide-batch",
        json={"message_ids": ["irrelevant"], "outcome": "edit_approve"},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 422


def test_decide_batch_with_no_reviewer_key_is_401(configured_reviewers, two_messages) -> None:
    message_ids, _campaign_id = two_messages
    response = TestClient(app).post(
        f"{REVIEWS}/decide-batch", json={"message_ids": message_ids, "outcome": "approve"}
    )
    assert response.status_code == 401


def test_list_reviews_pages_through_the_query_params(two_messages) -> None:
    message_ids, campaign_id = two_messages
    page_one = client.get(REVIEWS, params={"campaign_id": campaign_id, "limit": 1})
    assert page_one.status_code == 200
    body_one = page_one.json()
    assert [row["message_id"] for row in body_one["items"]] == [message_ids[0]]
    assert body_one["next_cursor"] is not None

    page_two = client.get(
        REVIEWS, params={"campaign_id": campaign_id, "limit": 1, "cursor": body_one["next_cursor"]}
    )
    assert page_two.status_code == 200
    body_two = page_two.json()
    assert [row["message_id"] for row in body_two["items"]] == [message_ids[1]]
    assert body_two["next_cursor"] is None


def test_list_reviews_total_count_covers_every_page_not_just_this_one(two_messages) -> None:
    message_ids, campaign_id = two_messages
    response = client.get(REVIEWS, params={"campaign_id": campaign_id, "limit": 1})
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["total_count"] == len(message_ids)


def test_list_reviews_defaults_to_oldest_first(two_messages) -> None:
    message_ids, campaign_id = two_messages
    response = client.get(REVIEWS, params={"campaign_id": campaign_id})
    assert response.status_code == 200
    ids = [row["message_id"] for row in response.json()["items"]]
    assert ids == message_ids


def test_list_reviews_newest_first_reverses_the_order(two_messages) -> None:
    message_ids, campaign_id = two_messages
    response = client.get(REVIEWS, params={"campaign_id": campaign_id, "order": "newest_first"})
    assert response.status_code == 200
    ids = [row["message_id"] for row in response.json()["items"]]
    assert ids == list(reversed(message_ids))


def test_list_reviews_newest_first_pages_through_the_query_params(two_messages) -> None:
    message_ids, campaign_id = two_messages
    page_one = client.get(
        REVIEWS, params={"campaign_id": campaign_id, "order": "newest_first", "limit": 1}
    )
    assert page_one.status_code == 200
    body_one = page_one.json()
    assert [row["message_id"] for row in body_one["items"]] == [message_ids[1]]
    assert body_one["next_cursor"] is not None

    page_two = client.get(
        REVIEWS,
        params={
            "campaign_id": campaign_id,
            "order": "newest_first",
            "limit": 1,
            "cursor": body_one["next_cursor"],
        },
    )
    assert page_two.status_code == 200
    body_two = page_two.json()
    assert [row["message_id"] for row in body_two["items"]] == [message_ids[0]]
    assert body_two["next_cursor"] is None


def test_list_reviews_rejects_an_unknown_order(two_messages) -> None:
    _message_ids, campaign_id = two_messages
    response = client.get(REVIEWS, params={"campaign_id": campaign_id, "order": "sideways"})
    assert response.status_code == 422
