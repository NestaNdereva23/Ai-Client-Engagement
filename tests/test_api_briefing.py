"""Tests for GET /briefing/{client_id}/{unit_fund_id}: that it re-attaches
a real name with no X-Reviewer-Key required (unlike GET /clients/{id}/name),
the 404-vs-data-missing distinction, and that both audit rows land on every
call, attributed to fa_id rather than a reviewer key.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

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

_SIGNALS = {
    "sig_heavy_withdrawal": True,
    "sig_dormant": True,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
    "sig_never_repeated": False,
}


@pytest.fixture
def seeded_client(db):
    with SessionLocal() as session:
        session.add(
            ClientRiskFeatures(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                balance_tier="Institutional",
                overdue_multiple=None,
                **_SIGNALS,
                risk_score=60,
                risk_band="High",
                risk_reasons="Heavy withdrawal; No deposit in 12 months",
                fund_at_risk=1_000_000.0,
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
                n_deposits=1,
                n_withdrawals=1,
                last_deposit_date=date(2024, 1, 1),
                last_withdrawal_date=date(2024, 6, 1),
                largest_withdrawal=200_000.0,
                deposit_count_capped=False,
                withdrawal_history_hidden=False,
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


def test_no_reviewer_key_needed_even_when_none_are_configured(
    unconfigured_reviewers, seeded_client
) -> None:
    """The one deliberate exception to the reviewer-key stopgap: unlike
    GET /clients/{id}/name, this succeeds with no X-Reviewer-Key header at
    all, even with an empty reviewer roster.
    """
    response = client.get(_url())
    assert response.status_code == 200


def test_unknown_client_fund_is_404(db) -> None:
    response = client.get(_url(999999, 999999))
    assert response.status_code == 404


def test_returns_the_rendered_briefing_and_the_real_name(seeded_client) -> None:
    response = client.get(_url())
    assert response.status_code == 200
    body = response.json()
    assert body["client_id"] == CLIENT_ID
    assert body["unit_fund_id"] == FUND_ID
    assert body["client_name"] == "Jane Doe"
    assert "CLIENT BRIEFING" in body["text"]
    assert "Risk 60/100 (High)   Route: fa_call_priority" in body["text"]
    assert "Heavy withdrawal" in body["text"]


def test_basis_lists_the_fired_signals_in_order(seeded_client) -> None:
    response = client.get(_url())
    assert response.status_code == 200
    # seeded_client fires sig_heavy_withdrawal and sig_dormant only (see
    # _SIGNALS), in SIGNAL_ORDER: broken_pattern, dormant, heavy_withdrawal, ...
    assert response.json()["basis"] == ["No deposit in 12 months", "Heavy withdrawal"]


def test_open_complaint_shows_the_caveat(seeded_client) -> None:
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

    response = client.get(_url())
    assert response.status_code == 200
    assert "this client has an open complaint" in response.json()["text"]


def test_both_audit_rows_are_written_on_every_call(seeded_client) -> None:
    response = client.get(_url())
    assert response.status_code == 200

    with SessionLocal() as session:
        view_rows = session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "risk_briefing",
                AuditLog.action == "view",
                AuditLog.entity_id == str(CLIENT_ID),
            )
            .order_by(AuditLog.log_id.desc())
        ).all()
        vault_rows = session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "pii_vault",
                AuditLog.action == "read",
                AuditLog.entity_id == str(CLIENT_ID),
            )
            .order_by(AuditLog.log_id.desc())
        ).all()

    assert view_rows, "expected a risk_briefing view audit row"
    assert view_rows[0].actor_id == "fa-77"
    assert "reviewer_id" not in view_rows[0].detail
    assert vault_rows, "expected a pii_vault read audit row"
    assert vault_rows[0].detail["purpose"] == "risk_briefing"
