"""Tests for the active-client console endpoints: logging an interaction
(fa_id attribution with no reviewer-key gate, the audit row, the 404 for an
unknown client-fund), reading the interaction history back, and the
profile read.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.active_clients import (
    ActiveClientFund,
    ActiveClientInteraction,
    ActiveTransaction,
)
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
    "sig_heavy_withdrawal": False,
    "sig_dormant": True,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
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
                n_deposits=2,
                n_withdrawals=0,
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
        session.execute(delete(ActiveTransaction).where(ActiveTransaction.client_id == CLIENT_ID))
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


def _post_interactions_url(
    client_id: int = CLIENT_ID, unit_fund_id: int = FUND_ID, fa_id: str = "fa-77"
) -> str:
    return f"{_interactions_url(client_id, unit_fund_id)}?fa_id={fa_id}"


def _profile_url(client_id: int = CLIENT_ID, unit_fund_id: int = FUND_ID) -> str:
    return f"/api/v1/active-clients/{client_id}/{unit_fund_id}/profile"


def _transactions_url(client_id: int = CLIENT_ID, unit_fund_id: int = FUND_ID) -> str:
    return f"/api/v1/active-clients/{client_id}/{unit_fund_id}/transactions"


def test_post_interaction_needs_no_reviewer_key_even_when_none_are_configured(
    unconfigured_reviewers, seeded_active_client
) -> None:
    """Same deliberate exception GET /briefing/... makes: this write
    succeeds with no X-Reviewer-Key header at all, even with an empty
    reviewer roster, because it's attributed by fa_id instead.
    """
    response = client.post(_post_interactions_url(), json={"type": "call_logged"})
    assert response.status_code == 201


def test_post_interaction_missing_fa_id_is_422(seeded_active_client) -> None:
    response = client.post(_interactions_url(), json={"type": "call_logged"})
    assert response.status_code == 422


def test_post_interaction_unknown_client_fund_is_404() -> None:
    response = client.post(
        _post_interactions_url(999999, 999999),
        json={"type": "call_logged"},
    )
    assert response.status_code == 404


def test_post_interaction_records_fa_id_not_a_resolved_reviewer_id(seeded_active_client) -> None:
    response = client.post(
        _post_interactions_url(fa_id="fa-1"),
        json={"type": "call_logged", "note": "left a voicemail"},
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


def test_post_interaction_accepts_email_sent(seeded_active_client) -> None:
    response = client.post(_post_interactions_url(), json={"type": "email_sent"})
    assert response.status_code == 201
    assert response.json()["type"] == "email_sent"


def test_post_interaction_risk_band_is_null_when_never_scored(seeded_active_client) -> None:
    response = client.post(_post_interactions_url(), json={"type": "call_logged"})
    assert response.status_code == 201
    assert response.json()["risk_band_at_interaction"] is None


def test_post_interaction_stamps_the_current_risk_band(seeded_active_client) -> None:
    with SessionLocal() as session:
        session.add(
            ClientRiskFeatures(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                balance_tier="Small",
                **_SIGNALS,
                risk_score=55,
                risk_band="High",
                risk_reasons="No deposit in 12 months",
                fund_at_risk=50_000.0,
                config_version=1,
                route="fa_call_priority",
                queue_rank=1,
            )
        )
        session.commit()

    try:
        response = client.post(_post_interactions_url(), json={"type": "dismissed"})
        assert response.status_code == 201
        assert response.json()["risk_band_at_interaction"] == "High"
    finally:
        with SessionLocal() as session:
            session.execute(
                delete(ClientRiskFeatures).where(
                    ClientRiskFeatures.client_id == CLIENT_ID,
                    ClientRiskFeatures.unit_fund_id == FUND_ID,
                )
            )
            session.commit()


def test_get_interactions_returns_most_recent_first(seeded_active_client) -> None:
    client.post(_post_interactions_url(fa_id="fa-1"), json={"type": "snoozed"})
    client.post(_post_interactions_url(fa_id="fa-2"), json={"type": "dismissed"})

    response = client.get(_interactions_url())
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert [row["type"] for row in body] == ["dismissed", "snoozed"]
    assert {row["reviewer_id"] for row in body} == {"fa-1", "fa-2"}


def test_get_interactions_since_filter_excludes_older_rows(seeded_active_client) -> None:
    client.post(_post_interactions_url(fa_id="fa-1"), json={"type": "snoozed"})

    far_future = (date.today() + timedelta(days=3650)).isoformat()
    response = client.get(_interactions_url(), params={"since": far_future})
    assert response.status_code == 200
    assert response.json() == []


def test_get_interactions_is_not_gated_by_the_reviewer_key(seeded_active_client) -> None:
    response = client.get(_interactions_url())
    assert response.status_code == 200


def test_get_profile_unknown_client_fund_is_404() -> None:
    response = client.get(_profile_url(999999, 999999))
    assert response.status_code == 404


def test_get_profile_returns_identity_and_bands(seeded_active_client) -> None:
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
                risk_reasons="No deposit in 12 months",
                fund_at_risk=50_000.0,
                signals=_SIGNALS,
                recency_band="1-2y",
                balance_tier="Small",
                value_tier="Medium",
            ),
            RouteResult(route="fa_call_priority", queue_rank=1, complaint_caveat=False),
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        session.add(
            ClientRiskFeatures(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                balance_tier="Small",
                **_SIGNALS,
                risk_score=55,
                risk_band="High",
                risk_reasons="No deposit in 12 months",
                fund_at_risk=50_000.0,
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
        session.add(
            ActiveTransaction(
                txn_id=9430100001,
                txn_type="purchase",
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                txn_date=date(2026, 1, 5),
                amount=10_000.0,
            )
        )
        session.commit()

    client.post(_post_interactions_url(fa_id="fa-1"), json={"type": "call_logged"})

    try:
        response = client.get(_profile_url())
        assert response.status_code == 200
        body = response.json()
        assert body["identity"]["client_id"] == CLIENT_ID
        assert body["identity"]["client_code"] == "C94301"
        assert body["identity"]["balance"] == 50_000.0
        assert body["identity"]["deposit_count_capped"] is False
        assert body["identity"]["withdrawal_history_hidden"] is False
        assert body["bands"]["risk_score"] == 55
        assert body["bands"]["risk_band"] == "High"
        assert body["bands"]["route"] == "fa_call_priority"
        # _SIGNALS fires only sig_dormant; SIGNAL_ORDER is broken_pattern,
        # dormant, heavy_withdrawal, ...
        assert body["bands"]["risk_reason_tags"] == ["dormant"]
        assert len(body["risk_history"]) == 1
        assert body["risk_history"][0]["run_id"] == run_id
        assert body["risk_history"][0]["risk_reason_tags"] == ["dormant"]
        assert len(body["complaints"]) == 1
        assert body["complaints"][0]["status"] == "open"
        assert len(body["interactions"]) == 1
        assert body["interactions"][0]["type"] == "call_logged"
        assert len(body["transactions"]) == 1
        assert body["transactions"][0]["txn_id"] == 9430100001
        assert body["transactions"][0]["amount"] == 10_000.0
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


def test_get_profile_reports_primary_signal_magnitude(
    configured_reviewers, seeded_active_client
) -> None:
    with SessionLocal() as session:
        row = session.get(ActiveClientFund, (CLIENT_ID, FUND_ID))
        row.last_deposit_date = date(2026, 1, 1)
        session.add(
            ClientRiskFeatures(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                balance_tier="Small",
                **_SIGNALS,
                risk_score=55,
                risk_band="High",
                risk_reasons="No deposit in 12 months",
                fund_at_risk=50_000.0,
                config_version=1,
                route="fa_call_priority",
                queue_rank=1,
            )
        )
        session.commit()

    try:
        response = client.get(_profile_url())
        assert response.status_code == 200
        magnitude = response.json()["bands"]["primary_signal_magnitude"]
        assert magnitude.startswith("No deposit in 12 months: ")
        assert "days since last deposit" in magnitude
    finally:
        with SessionLocal() as session:
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
    assert body["bands"]["risk_reason_tags"] == []
    assert body["bands"]["primary_signal_magnitude"] is None
    assert body["risk_history"] == []
    assert body["transactions"] == []


def test_get_transactions_returns_most_recent_first(
    configured_reviewers, seeded_active_client
) -> None:
    with SessionLocal() as session:
        session.add_all(
            [
                ActiveTransaction(
                    txn_id=9430100002,
                    txn_type="purchase",
                    client_id=CLIENT_ID,
                    unit_fund_id=FUND_ID,
                    txn_date=date(2026, 1, 1),
                    amount=5_000.0,
                ),
                ActiveTransaction(
                    txn_id=9430100003,
                    txn_type="sale",
                    client_id=CLIENT_ID,
                    unit_fund_id=FUND_ID,
                    txn_date=date(2026, 2, 1),
                    amount=1_000.0,
                    sale_type="partial_withdrawal",
                ),
            ]
        )
        session.commit()

    response = client.get(_transactions_url())
    assert response.status_code == 200
    body = response.json()
    assert [row["txn_id"] for row in body] == [9430100003, 9430100002]
    assert body[0]["sale_type"] == "partial_withdrawal"


def test_get_transactions_respects_limit(configured_reviewers, seeded_active_client) -> None:
    with SessionLocal() as session:
        session.add_all(
            [
                ActiveTransaction(
                    txn_id=9430100004,
                    txn_type="purchase",
                    client_id=CLIENT_ID,
                    unit_fund_id=FUND_ID,
                    txn_date=date(2026, 1, 1),
                    amount=1.0,
                ),
                ActiveTransaction(
                    txn_id=9430100005,
                    txn_type="purchase",
                    client_id=CLIENT_ID,
                    unit_fund_id=FUND_ID,
                    txn_date=date(2026, 1, 2),
                    amount=1.0,
                ),
            ]
        )
        session.commit()

    response = client.get(_transactions_url(), params={"limit": 1})
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_get_transactions_unknown_client_fund_is_empty_not_404() -> None:
    response = client.get(_transactions_url(999999, 999999))
    assert response.status_code == 200
    assert response.json() == []


def test_get_transactions_is_not_gated_by_the_reviewer_key(
    configured_reviewers, seeded_active_client
) -> None:
    response = client.get(_transactions_url())
    assert response.status_code == 200


# --- GET .../deposit-percentile ---------------------------------------------


def _percentile_url(client_id: int = CLIENT_ID, unit_fund_id: int = FUND_ID) -> str:
    return f"/api/v1/active-clients/{client_id}/{unit_fund_id}/deposit-percentile"


def test_deposit_percentile_404s_for_unknown_client_fund() -> None:
    response = client.get(_percentile_url(999999, 999999))
    assert response.status_code == 404


def test_deposit_percentile_sums_only_purchase_transactions(seeded_active_client) -> None:
    with SessionLocal() as session:
        session.add_all(
            [
                ActiveTransaction(
                    txn_id=9430100020,
                    txn_type="purchase",
                    client_id=CLIENT_ID,
                    unit_fund_id=FUND_ID,
                    txn_date=date(2026, 1, 1),
                    amount=7_000.0,
                ),
                ActiveTransaction(
                    txn_id=9430100021,
                    txn_type="sale",
                    client_id=CLIENT_ID,
                    unit_fund_id=FUND_ID,
                    txn_date=date(2026, 1, 2),
                    amount=3_000.0,
                ),
            ]
        )
        session.commit()

    response = client.get(_percentile_url())
    assert response.status_code == 200
    body = response.json()
    assert body["total_deposits"] == 7_000.0
    assert body["deposit_count_capped"] is False


def test_deposit_percentile_reflects_deposit_count_capped(seeded_active_client) -> None:
    with SessionLocal() as session:
        row = session.get(ActiveClientFund, (CLIENT_ID, FUND_ID))
        row.deposit_count_capped = True
        session.commit()

    response = client.get(_percentile_url())
    assert response.json()["deposit_count_capped"] is True


def test_deposit_percentile_rank_moves_with_a_higher_book_entry(seeded_active_client) -> None:
    with SessionLocal() as session:
        session.add(
            ActiveTransaction(
                txn_id=9430100022,
                txn_type="purchase",
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                txn_date=date(2026, 1, 1),
                amount=5_000.0,
            )
        )
        session.commit()

    before = client.get(_percentile_url()).json()

    higher_id, lower_id = 94390, 94391
    with SessionLocal() as session:
        session.add_all(
            [
                ActiveClientFund(
                    client_id=higher_id, unit_fund_id=FUND_ID, n_deposits=1, n_withdrawals=0
                ),
                ActiveClientFund(
                    client_id=lower_id, unit_fund_id=FUND_ID, n_deposits=1, n_withdrawals=0
                ),
            ]
        )
        session.commit()
        session.add_all(
            [
                ActiveTransaction(
                    txn_id=9430100023,
                    txn_type="purchase",
                    client_id=higher_id,
                    unit_fund_id=FUND_ID,
                    txn_date=date(2026, 1, 1),
                    amount=50_000.0,
                ),
                ActiveTransaction(
                    txn_id=9430100024,
                    txn_type="purchase",
                    client_id=lower_id,
                    unit_fund_id=FUND_ID,
                    txn_date=date(2026, 1, 1),
                    amount=1.0,
                ),
            ]
        )
        session.commit()

    try:
        after = client.get(_percentile_url()).json()
        assert after["book_size"] == before["book_size"] + 2
        # Only the higher entry lands ahead of the target's total.
        assert after["rank"] == before["rank"] + 1
    finally:
        with SessionLocal() as session:
            session.execute(
                delete(ActiveTransaction).where(
                    ActiveTransaction.client_id.in_([higher_id, lower_id])
                )
            )
            session.execute(
                delete(ActiveClientFund).where(
                    ActiveClientFund.client_id.in_([higher_id, lower_id])
                )
            )
            session.commit()


# --- GET /active-clients: the paginated roster ------------------------------

ROSTER = "/api/v1/active-clients"


@pytest.fixture
def seeded_roster(db):
    scored_id, unscored_id = 94350, 94351
    route = "roster_test_route_94350"
    with SessionLocal() as session:
        session.add_all(
            [
                ActiveClientFund(
                    client_id=scored_id,
                    unit_fund_id=FUND_ID,
                    balance=1_000.0,
                    n_deposits=1,
                    n_withdrawals=0,
                ),
                ActiveClientFund(
                    client_id=unscored_id,
                    unit_fund_id=FUND_ID,
                    balance=2_000.0,
                    n_deposits=1,
                    n_withdrawals=0,
                ),
            ]
        )
        session.commit()
        session.add(
            ClientRiskFeatures(
                client_id=scored_id,
                unit_fund_id=FUND_ID,
                balance_tier="Small",
                **_SIGNALS,
                risk_score=20,
                risk_band="Watch",
                risk_reasons="No deposit in 12 months",
                fund_at_risk=100.0,
                config_version=1,
                route=route,
                queue_rank=None,
            )
        )
        session.commit()

    yield scored_id, unscored_id, route

    with SessionLocal() as session:
        session.execute(
            delete(ClientRiskFeatures).where(
                ClientRiskFeatures.client_id.in_([scored_id, unscored_id])
            )
        )
        session.execute(
            delete(ActiveClientFund).where(ActiveClientFund.client_id.in_([scored_id, unscored_id]))
        )
        session.execute(
            delete(ActiveClientInteraction).where(
                ActiveClientInteraction.client_id.in_([scored_id, unscored_id])
            )
        )
        session.execute(
            delete(ClientComplaint).where(ClientComplaint.client_id.in_([scored_id, unscored_id]))
        )
        session.commit()


def test_roster_includes_an_unscored_row_with_null_bands(seeded_roster) -> None:
    _scored_id, unscored_id, _route = seeded_roster
    seen: dict[int, dict] = {}
    cursor = None
    for _ in range(20):
        params = {"limit": 50}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get(ROSTER, params=params)
        assert response.status_code == 200
        body = response.json()
        for row in body["items"]:
            seen[row["client_id"]] = row
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert unscored_id in seen
    assert seen[unscored_id]["risk_band"] is None


def test_roster_filters_by_route(seeded_roster) -> None:
    scored_id, _unscored_id, route = seeded_roster
    response = client.get(ROSTER, params={"route": route, "limit": 200})
    assert response.status_code == 200
    ids = {row["client_id"] for row in response.json()["items"]}
    assert ids == {scored_id}


def test_roster_invalid_cursor_is_400(db) -> None:
    response = client.get(ROSTER, params={"cursor": "not-a-cursor"})
    assert response.status_code == 400


def test_roster_filters_by_client_id(seeded_roster) -> None:
    scored_id, unscored_id, _route = seeded_roster
    response = client.get(ROSTER, params={"client_id": scored_id})
    assert response.status_code == 200
    ids = {row["client_id"] for row in response.json()["items"]}
    assert ids == {scored_id}
    assert unscored_id not in ids


def test_roster_reports_fired_signal_tags_and_empty_for_unscored(seeded_roster) -> None:
    scored_id, unscored_id, route = seeded_roster
    response = client.get(ROSTER, params={"route": route, "limit": 200})
    scored_row = next(r for r in response.json()["items"] if r["client_id"] == scored_id)
    assert scored_row["risk_reason_tags"] == ["dormant"]

    response = client.get(ROSTER, params={"client_id": unscored_id})
    unscored_row = response.json()["items"][0]
    assert unscored_row["risk_reason_tags"] == []


def test_roster_reports_client_code_fund_name_and_briefing_available(seeded_roster) -> None:
    scored_id, unscored_id, route = seeded_roster
    response = client.get(ROSTER, params={"route": route, "limit": 200})
    scored_row = next(r for r in response.json()["items"] if r["client_id"] == scored_id)
    # No Funds row is seeded for FUND_ID in these tests, so fund_name falls
    # back to "Fund {id}" -- the same fallback ActiveClientProfile uses.
    assert scored_row["fund_name"] == f"Fund {FUND_ID}"
    assert scored_row["client_code"] is None
    # scored_id has both a client_risk_features row and an active_client_fund
    # row, the same two-table existence check briefing_available_keys does,
    # so a briefing can render even without the finer feature measures.
    assert scored_row["briefing_available"] is True

    # unscored_id has no client_risk_features row at all, so a briefing
    # can't render.
    response = client.get(ROSTER, params={"client_id": unscored_id})
    unscored_row = response.json()["items"][0]
    assert unscored_row["briefing_available"] is False


def test_roster_reports_primary_signal_magnitude_and_none_for_unscored(seeded_roster) -> None:
    scored_id, unscored_id, route = seeded_roster
    response = client.get(ROSTER, params={"route": route, "limit": 200})
    scored_row = next(r for r in response.json()["items"] if r["client_id"] == scored_id)
    # _SIGNALS fires only sig_dormant, and no active feature measures are
    # seeded, so the label alone comes back with no magnitude number.
    assert scored_row["primary_signal_magnitude"] == "No deposit in 12 months"

    response = client.get(ROSTER, params={"client_id": unscored_id})
    unscored_row = response.json()["items"][0]
    assert unscored_row["primary_signal_magnitude"] is None


def test_roster_reports_complaint_caveat_and_last_interaction(seeded_roster) -> None:
    scored_id, unscored_id, _route = seeded_roster
    with SessionLocal() as session:
        session.add(
            ClientComplaint(
                client_id=scored_id,
                opened_at=date.today(),
                closed_at=None,
                status="open",
                category="service",
                channel="call",
            )
        )
        session.add(
            ActiveClientInteraction(
                client_id=scored_id,
                unit_fund_id=FUND_ID,
                type="call_logged",
                note=None,
                reviewer_id="fa-1",
            )
        )
        session.commit()

    response = client.get(ROSTER, params={"client_id": scored_id})
    scored_row = response.json()["items"][0]
    assert scored_row["complaint_caveat"] is True
    assert scored_row["last_interaction_at"] is not None

    response = client.get(ROSTER, params={"client_id": unscored_id})
    unscored_row = response.json()["items"][0]
    assert unscored_row["complaint_caveat"] is False
    assert unscored_row["last_interaction_at"] is None
