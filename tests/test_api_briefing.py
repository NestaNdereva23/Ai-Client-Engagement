"""Tests for GET /briefing/{client_id}/{unit_fund_id}: the reviewer-key
gate (re-attaches a name, same stopgap as GET /clients/{id}/name), the
404-vs-data-missing distinction, and that both audit rows land on every call.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api import reviewer_auth
from app.db.models.active_clients import ActiveClientFund
from app.db.models.audit import AuditLog
from app.db.models.complaints import ClientComplaint
from app.db.models.models import PiiVault
from app.db.models.risk import ClientRiskFeatures
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

FUND_ID = 940
CLIENT_ID = 94001

_TEST_REVIEWER_KEY = "test-reviewer-key"
REVIEWER_HEADERS = {"X-Reviewer-Key": _TEST_REVIEWER_KEY}

_SIGNALS = {
    "sig_drawdown": True,
    "sig_dormant": True,
    "sig_cadence_break": False,
    "sig_shrinking": False,
    "sig_fee_erosion": False,
    "sig_never_repeated": False,
}


class _ConfiguredReviewerSettings:
    reviewer_api_key = _TEST_REVIEWER_KEY


class _UnconfiguredReviewerSettings:
    reviewer_api_key = ""


@pytest.fixture
def configured_reviewer_key(monkeypatch):
    monkeypatch.setattr(reviewer_auth, "get_settings", lambda: _ConfiguredReviewerSettings())


@pytest.fixture
def seeded_client(db):
    with SessionLocal() as session:
        session.add(
            ClientRiskFeatures(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                balance_tier="Institutional",
                lapse_ratio=None,
                **_SIGNALS,
                risk_score=60,
                risk_band="High",
                risk_reasons="Heavy redemption; No contribution in 12m",
                aum_at_risk=1_000_000.0,
                config_version=1,
                route="fa_call_priority",
                queue_rank=1,
            )
        )
        session.add(
            ActiveClientFund(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                client_code="C94001",
                balance=1_000_000.0,
                n_purchases=1,
                n_sales=1,
                last_purchase=date(2024, 1, 1),
                last_real_sale_date=date(2024, 6, 1),
                largest_real_sale=200_000.0,
                purchases_censored=False,
                redemption_history_blind=False,
            )
        )
        session.add(PiiVault(client_id=CLIENT_ID, client_name="Jane Doe"))
        session.commit()
    yield CLIENT_ID
    with SessionLocal() as session:
        session.execute(
            delete(ClientRiskFeatures).where(
                ClientRiskFeatures.client_id == CLIENT_ID,
                ClientRiskFeatures.unit_fund_id == FUND_ID,
            )
        )
        session.execute(
            delete(ActiveClientFund).where(
                ActiveClientFund.client_id == CLIENT_ID, ActiveClientFund.unit_fund_id == FUND_ID
            )
        )
        session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT_ID))
        session.execute(delete(ClientComplaint).where(ClientComplaint.client_id == CLIENT_ID))
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type.in_(("risk_briefing", "pii_vault")),
                AuditLog.entity_id == str(CLIENT_ID),
            )
        )
        session.commit()


def _url(client_id: int = CLIENT_ID, unit_fund_id: int = FUND_ID) -> str:
    return f"/api/v1/briefing/{client_id}/{unit_fund_id}?fa_id=fa-77"


def test_missing_header_is_401(configured_reviewer_key, seeded_client) -> None:
    response = client.get(_url())
    assert response.status_code == 401


def test_wrong_key_is_401(configured_reviewer_key, seeded_client) -> None:
    response = client.get(_url(), headers={"X-Reviewer-Key": "wrong"})
    assert response.status_code == 401


def test_no_key_configured_is_503(monkeypatch, seeded_client) -> None:
    monkeypatch.setattr(reviewer_auth, "get_settings", lambda: _UnconfiguredReviewerSettings())
    response = client.get(_url(), headers=REVIEWER_HEADERS)
    assert response.status_code == 503


def test_unknown_client_fund_is_404(configured_reviewer_key, db) -> None:
    response = client.get(_url(999999, 999999), headers=REVIEWER_HEADERS)
    assert response.status_code == 404


def test_returns_the_rendered_briefing_and_the_real_name(
    configured_reviewer_key, seeded_client
) -> None:
    response = client.get(_url(), headers=REVIEWER_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["client_id"] == CLIENT_ID
    assert body["unit_fund_id"] == FUND_ID
    assert body["client_name"] == "Jane Doe"
    assert "CLIENT BRIEFING" in body["text"]
    assert "Risk 60/100 (High)   Route: fa_call_priority" in body["text"]
    assert "Heavy redemption" in body["text"]


def test_open_complaint_shows_the_caveat(configured_reviewer_key, seeded_client) -> None:
    with SessionLocal() as session:
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

    response = client.get(_url(), headers=REVIEWER_HEADERS)
    assert response.status_code == 200
    assert "this client has an open complaint" in response.json()["text"]


def test_both_audit_rows_are_written_on_every_call(configured_reviewer_key, seeded_client) -> None:
    response = client.get(_url(), headers=REVIEWER_HEADERS)
    assert response.status_code == 200

    with SessionLocal() as session:
        view_rows = session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "risk_briefing",
                AuditLog.action == "view",
                AuditLog.entity_id == str(CLIENT_ID),
            )
        ).all()
        vault_rows = session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "pii_vault",
                AuditLog.action == "read",
                AuditLog.entity_id == str(CLIENT_ID),
            )
        ).all()

    assert view_rows, "expected a risk_briefing view audit row"
    assert view_rows[-1].actor_id == "fa-77"
    assert vault_rows, "expected a pii_vault read audit row"
    assert vault_rows[-1].detail["purpose"] == "risk_briefing"
