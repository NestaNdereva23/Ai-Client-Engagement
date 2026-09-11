"""The agent insights HTTP API: list, count, open one, decide, recount.

Drives the router through TestClient against the real app, so these prove
the wiring rather than re-testing the state machine already covered in
test_agent_insight_state.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.agent_insight import AgentInsight, AgentInsightClient, AgentInsightFact
from app.db.models.audit import AuditLog
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

INSIGHTS = "/api/v1/agent/insights"

FUND_ID = 9701
CLIENT_A = 970101
CLIENT_B = 970102


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


@pytest.fixture
def insight(db: None):
    with SessionLocal() as session:
        row = AgentInsight(
            kind="risk",
            title="fees will empty a group of small accounts",
            group_name="fees_will_empty",
            group_definition={"months_until_empty_below": 6.0},
            client_count=2,
            money_total_kes=4_500.0,
            confidence="medium",
            confidence_reason="the balances are small so the estimate moves easily",
            suggestion="call them before the fee runs the balance down",
            avoid_saying="do not promise the account will stay open",
            why_now="the fee is charged again at the end of the month",
        )
        session.add(row)
        session.flush()
        insight_id = row.insight_id
        session.add_all(
            [
                AgentInsightFact(
                    insight_id=insight_id,
                    fact_text="client funds the fee will empty soon",
                    fact_value="2",
                    source_filter={"group_name": "fees_will_empty"},
                    source_table="active_client_fund",
                ),
                AgentInsightFact(
                    insight_id=insight_id,
                    fact_text="money held across the group",
                    fact_value="4500",
                    source_filter={"hand_written": True},
                    source_table="active_client_fund",
                ),
                AgentInsightFact(
                    insight_id=insight_id,
                    fact_text="client funds the agent counted for itself",
                    fact_value="0",
                    source_filter={
                        "conditions": [{"field": "risk_band", "op": "eq", "value": "No Such Band"}]
                    },
                    source_table="client_risk_features",
                ),
                AgentInsightClient(insight_id=insight_id, client_id=CLIENT_A, unit_fund_id=FUND_ID),
                AgentInsightClient(insight_id=insight_id, client_id=CLIENT_B, unit_fund_id=FUND_ID),
            ]
        )
        session.commit()

    yield insight_id

    with SessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.entity_id == str(insight_id)))
        session.execute(delete(AgentInsightFact).where(AgentInsightFact.insight_id == insight_id))
        session.execute(
            delete(AgentInsightClient).where(AgentInsightClient.insight_id == insight_id)
        )
        session.execute(delete(AgentInsight).where(AgentInsight.insight_id == insight_id))
        session.commit()


def _fact_ids(insight_id: int) -> list[int]:
    body = client.get(f"{INSIGHTS}/{insight_id}").json()
    return [fact["fact_id"] for fact in body["facts"]]


def test_list_insights_returns_counts_and_never_client_ids(insight: int) -> None:
    response = client.get(INSIGHTS, params={"state": "new"})
    assert response.status_code == 200
    body = response.json()
    row = next(item for item in body["items"] if item["insight_id"] == insight)
    assert row["kind"] == "risk"
    assert row["client_count"] == 2
    assert row["money_total_kes"] == 4_500.0
    assert row["confidence"] == "medium"
    assert "client_id" not in row
    assert body["total_count"] >= 1


def test_list_insights_filters_by_kind(insight: int) -> None:
    matching = client.get(INSIGHTS, params={"kind": "risk"}).json()["items"]
    assert insight in [item["insight_id"] for item in matching]

    other = client.get(INSIGHTS, params={"kind": "opportunity"}).json()["items"]
    assert insight not in [item["insight_id"] for item in other]


def test_list_insights_filters_by_state(insight: int) -> None:
    response = client.get(INSIGHTS, params={"state": "dismissed"})
    assert response.status_code == 200
    assert insight not in [item["insight_id"] for item in response.json()["items"]]


def test_list_insights_rejects_a_broken_cursor(insight: int) -> None:
    response = client.get(INSIGHTS, params={"cursor": "not-a-cursor"})
    assert response.status_code == 400


def test_counts_by_kind_counts_every_page(insight: int) -> None:
    response = client.get(f"{INSIGHTS}/counts", params={"state": "new"})
    assert response.status_code == 200
    body = response.json()
    assert body["counts_by_kind"]["risk"] >= 1
    assert body["total_count"] == sum(body["counts_by_kind"].values())


def test_get_insight_separates_facts_from_the_reading(insight: int) -> None:
    response = client.get(f"{INSIGHTS}/{insight}")
    assert response.status_code == 200
    body = response.json()
    assert body["why_now"] == "the fee is charged again at the end of the month"
    assert body["avoid_saying"] == "do not promise the account will stay open"
    assert body["confidence_reason"]
    assert body["listed_client_count"] == 2
    assert [fact["fact_value"] for fact in body["facts"]] == ["2", "4500", "0"]
    assert body["facts"][0]["source_filter"] == {"group_name": "fees_will_empty"}
    assert body["facts"][0]["source_table"] == "active_client_fund"


def test_get_insight_404s_when_missing(db: None) -> None:
    assert client.get(f"{INSIGHTS}/0").status_code == 404


def test_accept_records_who_decided(insight: int) -> None:
    response = client.post(
        f"{INSIGHTS}/{insight}/decision",
        json={"decision": "accept", "reason": "worth a call"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "accepted"
    assert body["decided_by"]
    assert body["decided_at"] is not None


def test_dismiss_keeps_the_reason(insight: int) -> None:
    response = client.post(
        f"{INSIGHTS}/{insight}/decision",
        json={"decision": "dismiss", "reason": "already covered by the call list"},
    )
    assert response.status_code == 200
    assert response.json()["dismissed_reason"] == "already covered by the call list"


def test_dismiss_needs_a_reason(insight: int) -> None:
    response = client.post(
        f"{INSIGHTS}/{insight}/decision", json={"decision": "dismiss", "reason": ""}
    )
    assert response.status_code == 422

    missing = client.post(f"{INSIGHTS}/{insight}/decision", json={"decision": "dismiss"})
    assert missing.status_code == 422


def test_a_decided_insight_cannot_be_decided_again(insight: int) -> None:
    first = client.post(
        f"{INSIGHTS}/{insight}/decision",
        json={"decision": "dismiss", "reason": "not useful"},
    )
    assert first.status_code == 200

    second = client.post(
        f"{INSIGHTS}/{insight}/decision",
        json={"decision": "accept", "reason": "changed my mind"},
    )
    assert second.status_code == 409


def test_recount_runs_a_group_filter_again(insight: int) -> None:
    fact_id = _fact_ids(insight)[0]
    response = client.get(f"{INSIGHTS}/{insight}/facts/{fact_id}/recount")
    assert response.status_code == 200
    body = response.json()
    assert body["stored_value"] == "2"
    assert body["group_name"] == "fees_will_empty"
    if body["can_recount"]:
        assert body["client_count"] is not None
    else:
        assert body["reason"]


def test_recount_refuses_a_filter_it_cannot_run(insight: int) -> None:
    fact_id = _fact_ids(insight)[1]
    response = client.get(f"{INSIGHTS}/{insight}/facts/{fact_id}/recount")
    assert response.status_code == 200
    body = response.json()
    assert body["can_recount"] is False
    assert body["client_count"] is None
    assert body["reason"]


def test_recount_404s_for_a_fact_on_another_insight(insight: int) -> None:
    response = client.get(f"{INSIGHTS}/{insight}/facts/0/recount")
    assert response.status_code == 404


def test_recount_runs_a_filter_the_agent_wrote_itself(insight: int) -> None:
    fact_id = _fact_ids(insight)[2]
    response = client.get(f"{INSIGHTS}/{insight}/facts/{fact_id}/recount")
    assert response.status_code == 200
    body = response.json()
    assert body["can_recount"] is True
    assert body["client_count"] == 0
