"""Tests for the active-client console endpoints: logging an interaction
(the reviewer-key gate, the audit row, the 404 for an unknown client-fund),
reading the interaction history back, and the profile read.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.audit import AuditLog
from app.db.models.complaints import ClientComplaint
from app.db.models.risk import ClientRiskFeatures, RiskRun, RiskSnapshot
from app.db.session import SessionLocal
from app.main import app
from app.risk.history import write_snapshot
from app.risk.routing import RouteResult
from app.risk.scoring import ScoreResult

client = TestClient(app)

FUND_ID = 943
CLIENT_ID = 94301

_SIGNALS = {
    "sig_drawdown": False,
    "sig_dormant": True,
    "sig_cadence_break": False,
    "sig_shrinking": False,
    "sig_fee_erosion": False,
    "sig_never_repeated": False,
}


@pytest.fixture
def seeded_active_client(db):
    with SessionLocal() as session:
        session.add(
            ActiveClientFund(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                client_code="C94301",
                balance=50_000.0,
                n_purchases=2,
                n_sales=0,
            )
        )
        session.commit()
    yield CLIENT_ID
    with SessionLocal() as session:
        session.execute(
            delete(ActiveClientInteraction).where(
                ActiveClientInteraction.client_id == CLIENT_ID,
                ActiveClientInteraction.unit_fund_id == FUND_ID,
            )
        )
        session.execute(
            delete(ActiveClientFund).where(
                ActiveClientFund.client_id == CLIENT_ID, ActiveClientFund.unit_fund_id == FUND_ID
            )
        )
        session.execute(delete(ClientComplaint).where(ClientComplaint.client_id == CLIENT_ID))
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "active_client_interaction",
                AuditLog.entity_id == f"{CLIENT_ID}/{FUND_ID}",
            )
        )
        session.commit()


def _interactions_url(client_id: int = CLIENT_ID, unit_fund_id: int = FUND_ID) -> str:
    return f"/api/v1/active-clients/{client_id}/{unit_fund_id}/interactions"


def _profile_url(client_id: int = CLIENT_ID, unit_fund_id: int = FUND_ID) -> str:
    return f"/api/v1/active-clients/{client_id}/{unit_fund_id}/profile"


def test_post_interaction_missing_header_is_401(configured_reviewers, seeded_active_client) -> None:
    response = client.post(_interactions_url(), json={"type": "call_logged"})
    assert response.status_code == 401


def test_post_interaction_no_key_configured_is_503(
    unconfigured_reviewers, seeded_active_client, reviewer_1_headers
) -> None:
    response = client.post(
        _interactions_url(), json={"type": "call_logged"}, headers=reviewer_1_headers
    )
    assert response.status_code == 503


def test_post_interaction_unknown_client_fund_is_404(
    configured_reviewers, reviewer_1_headers
) -> None:
    response = client.post(
        _interactions_url(999999, 999999),
        json={"type": "call_logged"},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 404


def test_post_interaction_records_reviewer_id_not_the_body(
    configured_reviewers, seeded_active_client, reviewer_1_headers
) -> None:
    response = client.post(
        _interactions_url(),
        json={"type": "call_logged", "note": "left a voicemail"},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["client_id"] == CLIENT_ID
    assert body["unit_fund_id"] == FUND_ID
    assert body["type"] == "call_logged"
    assert body["note"] == "left a voicemail"
    assert body["reviewer_id"] == "fa-1"

    with SessionLocal() as session:
        row = session.get(ActiveClientInteraction, body["id"])
        assert row is not None
        assert row.reviewer_id == "fa-1"

        audit = (
            session.query(AuditLog)
            .filter_by(entity_type="active_client_interaction", entity_id=f"{CLIENT_ID}/{FUND_ID}")
            .one()
        )
        assert audit.actor_id == "fa-1"
        assert audit.action == "call_logged"


def test_get_interactions_returns_most_recent_first(
    configured_reviewers, seeded_active_client, reviewer_1_headers, reviewer_2_headers
) -> None:
    client.post(_interactions_url(), json={"type": "snoozed"}, headers=reviewer_1_headers)
    client.post(_interactions_url(), json={"type": "dismissed"}, headers=reviewer_2_headers)

    response = client.get(_interactions_url())
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert [row["type"] for row in body] == ["dismissed", "snoozed"]
    assert {row["reviewer_id"] for row in body} == {"fa-1", "fa-2"}


def test_get_interactions_since_filter_excludes_older_rows(
    configured_reviewers, seeded_active_client, reviewer_1_headers
) -> None:
    client.post(_interactions_url(), json={"type": "snoozed"}, headers=reviewer_1_headers)

    far_future = (date.today() + timedelta(days=3650)).isoformat()
    response = client.get(_interactions_url(), params={"since": far_future})
    assert response.status_code == 200
    assert response.json() == []


def test_get_interactions_is_not_gated_by_the_reviewer_key(
    configured_reviewers, seeded_active_client
) -> None:
    response = client.get(_interactions_url())
    assert response.status_code == 200


def test_get_profile_unknown_client_fund_is_404() -> None:
    response = client.get(_profile_url(999999, 999999))
    assert response.status_code == 404


def test_get_profile_returns_identity_and_bands(
    configured_reviewers, seeded_active_client, reviewer_1_headers
) -> None:
    run_id = "profile-test-run-943"
    with SessionLocal() as session:
        session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
        session.flush()
        write_snapshot(
            session,
            run_id,
            CLIENT_ID,
            FUND_ID,
            ScoreResult(
                risk_score=55,
                risk_band="High",
                risk_reasons="No contribution in 12m",
                aum_at_risk=50_000.0,
                signals=_SIGNALS,
                recency_band="1-2y",
                balance_tier="Small",
                value_tier="Medium",
            ),
            RouteResult(route="fa_call_priority", queue_rank=1, complaint_caveat=False),
            config_version=1,
            credible_rhythm=True,
            lapse_ratio=1.0,
        )
        session.add(
            ClientRiskFeatures(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                balance_tier="Small",
                **_SIGNALS,
                risk_score=55,
                risk_band="High",
                risk_reasons="No contribution in 12m",
                aum_at_risk=50_000.0,
                config_version=1,
                route="fa_call_priority",
                queue_rank=1,
            )
        )
        session.add(
            ClientComplaint(
                client_id=CLIENT_ID,
                opened_at=date(2026, 1, 1),
                status="open",
                category="service",
                channel="call",
            )
        )
        session.commit()

    client.post(_interactions_url(), json={"type": "call_logged"}, headers=reviewer_1_headers)

    try:
        response = client.get(_profile_url())
        assert response.status_code == 200
        body = response.json()
        assert body["identity"]["client_id"] == CLIENT_ID
        assert body["identity"]["client_code"] == "C94301"
        assert body["bands"]["risk_score"] == 55
        assert body["bands"]["risk_band"] == "High"
        assert body["bands"]["route"] == "fa_call_priority"
        assert len(body["risk_history"]) == 1
        assert body["risk_history"][0]["run_id"] == run_id
        assert len(body["complaints"]) == 1
        assert body["complaints"][0]["status"] == "open"
        assert len(body["interactions"]) == 1
        assert body["interactions"][0]["type"] == "call_logged"
    finally:
        with SessionLocal() as session:
            session.execute(delete(RiskSnapshot).where(RiskSnapshot.run_id == run_id))
            session.execute(delete(RiskRun).where(RiskRun.run_id == run_id))
            session.execute(
                delete(ClientRiskFeatures).where(
                    ClientRiskFeatures.client_id == CLIENT_ID,
                    ClientRiskFeatures.unit_fund_id == FUND_ID,
                )
            )
            session.commit()


def test_get_profile_bands_are_null_without_a_risk_row(
    configured_reviewers, seeded_active_client
) -> None:
    response = client.get(_profile_url())
    assert response.status_code == 200
    body = response.json()
    assert body["bands"]["risk_score"] is None
    assert body["bands"]["route"] is None
    assert body["risk_history"] == []
