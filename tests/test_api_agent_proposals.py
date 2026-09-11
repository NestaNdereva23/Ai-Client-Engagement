"""The agent proposals HTTP API: list, open one, and decide.

Drives the router through TestClient against the real app, so these prove
the wiring rather than re-testing the service logic already covered in
test_services_agent_proposals.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.audit import AuditLog
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

PROPOSALS = "/api/v1/agent/proposals"

FUND_ID = 9601
INCLUDED_CLIENT = 960101
SKIPPED_CLIENT = 960102


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


@pytest.fixture
def proposal(db: None):
    with SessionLocal() as session:
        row = AgentProposal(
            action_code="start_win_back",
            catalog_version=1,
            group_name="very small and quiet",
            client_count=2,
            money_total_kes=4_500.0,
            evidence="two dormant clients with a small, quiet balance",
            reason="win back before they close the account",
            permission_applied="suggest_only",
        )
        session.add(row)
        session.flush()
        session.add_all(
            [
                AgentProposalClient(
                    proposal_id=row.proposal_id,
                    client_id=INCLUDED_CLIENT,
                    unit_fund_id=FUND_ID,
                    included=True,
                ),
                AgentProposalClient(
                    proposal_id=row.proposal_id,
                    client_id=SKIPPED_CLIENT,
                    unit_fund_id=FUND_ID,
                    included=False,
                    skip_reason="open_complaint",
                ),
            ]
        )
        session.commit()
        proposal_id = row.proposal_id

    yield proposal_id

    with SessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.entity_id == str(proposal_id)))
        session.execute(
            delete(AgentProposalClient).where(AgentProposalClient.proposal_id == proposal_id)
        )
        session.execute(delete(AgentProposal).where(AgentProposal.proposal_id == proposal_id))
        session.commit()


def test_list_proposals_returns_counts_and_money_never_names(proposal: int) -> None:
    response = client.get(PROPOSALS, params={"status": "proposed"})
    assert response.status_code == 200
    body = response.json()
    row = next(item for item in body["items"] if item["proposal_id"] == proposal)
    assert row["client_count"] == 2
    assert row["included_count"] == 1
    assert row["money_total_kes"] == 4_500.0
    assert row["group_name"] == "very small and quiet"
    assert "client_id" not in row
    assert body["total_count"] >= 1


def test_list_proposals_filters_by_status(proposal: int) -> None:
    response = client.get(PROPOSALS, params={"status": "rejected"})
    assert response.status_code == 200
    ids = [item["proposal_id"] for item in response.json()["items"]]
    assert proposal not in ids


def test_list_proposals_filters_by_action_code(proposal: int) -> None:
    response = client.get(PROPOSALS, params={"action_code": "start_win_back"})
    assert response.status_code == 200
    ids = [item["proposal_id"] for item in response.json()["items"]]
    assert proposal in ids


def test_list_proposals_excludes_an_action_code(proposal: int) -> None:
    response = client.get(PROPOSALS, params={"exclude_action_code": "start_win_back"})
    assert response.status_code == 200
    ids = [item["proposal_id"] for item in response.json()["items"]]
    assert proposal not in ids


def test_get_proposal_returns_the_breakdown_of_who_was_left_out(proposal: int) -> None:
    response = client.get(f"{PROPOSALS}/{proposal}")
    assert response.status_code == 200
    body = response.json()
    assert body["evidence"]
    assert body["reason"]
    assert body["included_count"] == 1
    by_client = {row["client_id"]: row for row in body["clients"]}
    assert by_client[INCLUDED_CLIENT]["included"] is True
    assert by_client[SKIPPED_CLIENT]["included"] is False
    assert by_client[SKIPPED_CLIENT]["skip_reason"] == "open_complaint"


def test_get_proposal_404_when_missing(db: None) -> None:
    response = client.get(f"{PROPOSALS}/0")
    assert response.status_code == 404


def test_decide_approve_records_the_reviewer_and_reason(proposal: int) -> None:
    response = client.post(
        f"{PROPOSALS}/{proposal}/decision",
        json={"decision": "approve", "reason": "looks right, send it"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == "fa-1"
    assert body["decided_at"] is not None

    with SessionLocal() as session:
        row = session.get(AgentProposal, proposal)
    assert row.status == "approved"


def test_decide_reject_records_the_reason(proposal: int) -> None:
    response = client.post(
        f"{PROPOSALS}/{proposal}/decision",
        json={"decision": "reject", "reason": "not a good fit tonight"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"

    with SessionLocal() as session:
        audit_row = session.scalar(
            select(AuditLog)
            .where(AuditLog.entity_id == str(proposal), AuditLog.action == "transition")
            .order_by(AuditLog.created_at.desc())
        )
    assert audit_row.detail["to"] == "rejected"
    assert audit_row.detail["reason"] == "not a good fit tonight"


def test_decide_twice_is_refused(proposal: int) -> None:
    first = client.post(
        f"{PROPOSALS}/{proposal}/decision", json={"decision": "approve", "reason": "first pass"}
    )
    assert first.status_code == 200

    second = client.post(
        f"{PROPOSALS}/{proposal}/decision", json={"decision": "reject", "reason": "second pass"}
    )
    assert second.status_code == 409


def test_decide_requires_a_reason(proposal: int) -> None:
    response = client.post(f"{PROPOSALS}/{proposal}/decision", json={"decision": "approve"})
    assert response.status_code == 422


def test_decide_404_when_missing(db: None) -> None:
    response = client.post(
        f"{PROPOSALS}/0/decision", json={"decision": "approve", "reason": "does not exist"}
    )
    assert response.status_code == 404
