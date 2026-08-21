"""Tests for GET /briefing/{client_id}/{unit_fund_id}: that it re-attaches
a real name with no X-Reviewer-Key required (unlike GET /clients/{id}/name),
the 404-vs-data-missing distinction, and that both audit rows land on every
call, attributed to username rather than a reviewer key.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.api.routers.briefing as briefing_router
from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.audit import AuditLog
from app.db.models.briefing import BriefingNarrative
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
        # A narration stored by one test is served to the next one asking
        # for the same client, which is the point of storing it and exactly
        # what must not leak across tests.
        session.execute(delete(BriefingNarrative).where(BriefingNarrative.client_id == CLIENT_ID))
        session.execute(delete(ClientComplaint).where(ClientComplaint.client_id == CLIENT_ID))
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type.in_(("risk_briefing", "pii_vault")),
                AuditLog.entity_id == str(CLIENT_ID),
            )
        )
        session.commit()


class _ScriptedLLMClient:
    model = "briefing-stub"

    def __init__(self, response: str) -> None:
        self._response = response

    def generate(self, *, system: str, user: str) -> str:
        return self._response


@pytest.fixture
def ai_briefing_enabled(monkeypatch):
    """Turn settings.ai_briefing_enabled on for one test, through the real
    config path (env var + cache_clear) so every module that calls
    get_settings() -- the router and the service both -- agrees, rather
    than needing each module's own imported binding patched separately.
    """
    monkeypatch.setenv("AI_BRIEFING_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def ai_briefing_disabled(monkeypatch):
    """The mirror of ai_briefing_enabled, set explicitly rather than left to
    the schema default: a developer .env that turns the feature on would
    otherwise make the off case call a real model.
    """
    monkeypatch.setenv("AI_BRIEFING_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _url(client_id: int = CLIENT_ID, unit_fund_id: int = FUND_ID) -> str:
    return f"/api/v1/briefing/{client_id}/{unit_fund_id}?username=fa-77"


def _narrative_url(client_id: int = CLIENT_ID, unit_fund_id: int = FUND_ID) -> str:
    return f"/api/v1/briefing/{client_id}/{unit_fund_id}/narrative?username=fa-77"


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


# --- GET /briefing/{id}/{fund}/narrative (AM15) ------------------------------


def test_narrative_route_404s_when_the_feature_is_off(ai_briefing_disabled, seeded_client) -> None:
    """Off is the default (config.py::ai_briefing_enabled): the route
    behaves as though it does not exist, the deterministic briefing above
    stays the only one there is.
    """
    response = client.get(_narrative_url())
    assert response.status_code == 404
    assert "not enabled" in response.json()["error"]["message"]
    assert response.json()["error"]["code"] == "narrative_disabled"


def test_narrative_route_404s_differently_when_the_client_is_unknown(
    ai_briefing_enabled, db
) -> None:
    """The other 404 on this route keeps the plain not_found code, so a
    caller can tell a switched-off feature from a client we have no data on
    without reading the message.
    """
    response = client.get(_narrative_url(999999, 999999))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_narrative_route_returns_a_clean_narrative_when_enabled(
    monkeypatch, ai_briefing_enabled, seeded_client
) -> None:
    narrative_text = "This client has gone quiet and broke their own pattern recently."
    monkeypatch.setattr(
        briefing_router,
        "get_briefing_llm_client",
        lambda settings: _ScriptedLLMClient(narrative_text),
    )

    response = client.get(_narrative_url())

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "narrative"
    assert body["text"] == narrative_text
    assert body["client_name"] == "Jane Doe"
    assert body["basis"] == ["No deposit in 12 months", "Heavy withdrawal"]


def test_narrative_route_falls_back_to_the_deterministic_text_on_an_ungrounded_reply(
    monkeypatch, ai_briefing_enabled, seeded_client
) -> None:
    ungrounded = "This client's balance is down 42% since last quarter."
    monkeypatch.setattr(
        briefing_router, "get_briefing_llm_client", lambda settings: _ScriptedLLMClient(ungrounded)
    )

    response = client.get(_narrative_url())

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "deterministic_fallback"
    assert "CLIENT BRIEFING" in body["text"]
    assert ungrounded not in body["text"]


def test_narrative_view_is_audited_with_its_mode(
    monkeypatch, ai_briefing_enabled, seeded_client
) -> None:
    monkeypatch.setattr(
        briefing_router,
        "get_briefing_llm_client",
        lambda settings: _ScriptedLLMClient("A short clean narrative with no figures at all."),
    )

    response = client.get(_narrative_url())
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
        crossing_rows = session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "risk_briefing",
                AuditLog.action == "narrate",
                AuditLog.entity_id == str(CLIENT_ID),
            )
            .order_by(AuditLog.log_id.desc())
        ).all()

    assert view_rows, "expected a risk_briefing view audit row"
    assert view_rows[0].detail["mode"] == "narrative"
    assert crossing_rows, "expected a risk_briefing narrate (boundary) audit row"
    assert crossing_rows[0].detail["inbound"] == "pass"
    assert crossing_rows[0].detail["outbound"] == "pass"
