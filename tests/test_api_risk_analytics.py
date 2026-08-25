"""Tests for GET /risk/analytics: coverage plus risk-band, route,
balance-tier, value-tier, and recency-band distribution, signal-fire
frequency, and primary-signal distribution, all read off
client_risk_features.

Every count is read as a delta against a baseline taken before each test
seeds its own rows, the same approach test_api_risk_coverage.py uses, so the
numbers stay correct no matter what other data already lives in the shared
test database.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.risk import ClientRiskFeatures
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


FUND_ID = 945

_ALL_FALSE = {
    "sig_heavy_withdrawal": False,
    "sig_dormant": False,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
    "sig_never_repeated": False,
}


def _row(
    client_id: int,
    *,
    risk_band: str,
    route: str | None,
    signals: dict,
    balance_tier: str = "Small",
    value_tier: str | None = None,
    recency_band: str | None = None,
) -> ClientRiskFeatures:
    return ClientRiskFeatures(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance_tier=balance_tier,
        value_tier=value_tier,
        recency_band=recency_band,
        **signals,
        risk_score=30,
        risk_band=risk_band,
        risk_reasons="No deposit in 12 months",
        fund_at_risk=100.0,
        config_version=1,
        route=route,
        queue_rank=None,
    )


def _analytics() -> dict:
    response = client.get("/api/v1/risk/analytics")
    assert response.status_code == 200
    return response.json()


def _count(body: dict, section: str, key: str | None) -> int:
    return next((row["count"] for row in body[section] if row["key"] == key), 0)


@pytest.fixture
def cleanup():
    client_ids: list[int] = []
    yield client_ids
    with SessionLocal() as session:
        session.execute(
            delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(client_ids))
        )
        session.commit()


def test_shape_includes_coverage_and_all_breakdowns() -> None:
    body = _analytics()
    assert isinstance(body["book_size"], int)
    assert isinstance(body["scored_count"], int)
    assert "as_of" in body
    assert isinstance(body["by_risk_band"], list)
    assert isinstance(body["by_route"], list)
    assert isinstance(body["by_balance_tier"], list)
    assert isinstance(body["by_value_tier"], list)
    assert isinstance(body["by_recency_band"], list)
    assert isinstance(body["signal_frequency"], list)
    assert isinstance(body["primary_signal_distribution"], list)
    assert isinstance(body["total_fund_at_risk"], float)


def test_total_fund_at_risk_reflects_a_newly_scored_client(cleanup) -> None:
    client_ids = cleanup
    client_id = 94504
    client_ids.append(client_id)
    before = _analytics()["total_fund_at_risk"]

    with SessionLocal() as session:
        row = _row(client_id, risk_band="High", route=None, signals=_ALL_FALSE)
        row.fund_at_risk = 2_500.0
        session.add(row)
        session.commit()

    after = _analytics()["total_fund_at_risk"]
    assert after == pytest.approx(before + 2_500.0)


def test_by_risk_band_reflects_a_newly_scored_client(cleanup) -> None:
    client_ids = cleanup
    client_id = 94501
    client_ids.append(client_id)
    before = _analytics()

    with SessionLocal() as session:
        session.add(
            _row(client_id, risk_band="Critical", route="fa_call_priority", signals=_ALL_FALSE)
        )
        session.commit()

    after = _analytics()
    assert (
        _count(after, "by_risk_band", "Critical") == _count(before, "by_risk_band", "Critical") + 1
    )


def test_by_route_reflects_a_newly_scored_client(cleanup) -> None:
    client_ids = cleanup
    client_id = 94502
    client_ids.append(client_id)
    route = "risk_analytics_test_route"
    before = _analytics()

    with SessionLocal() as session:
        session.add(_row(client_id, risk_band="Watch", route=route, signals=_ALL_FALSE))
        session.commit()

    after = _analytics()
    assert _count(after, "by_route", route) == _count(before, "by_route", route) + 1


def test_signal_frequency_reflects_a_fired_signal(cleanup) -> None:
    client_ids = cleanup
    client_id = 94503
    client_ids.append(client_id)
    signals = dict(_ALL_FALSE)
    signals["sig_dormant"] = True
    before = _analytics()

    with SessionLocal() as session:
        session.add(_row(client_id, risk_band="High", route=None, signals=signals))
        session.commit()

    after = _analytics()
    assert (
        _count(after, "signal_frequency", "dormant")
        == _count(before, "signal_frequency", "dormant") + 1
    )
    # An unfired signal on this same row must not move.
    assert _count(after, "signal_frequency", "shrinking") == _count(
        before, "signal_frequency", "shrinking"
    )


def test_by_balance_value_and_recency_band_reflect_a_newly_scored_client(cleanup) -> None:
    client_ids = cleanup
    client_id = 94505
    client_ids.append(client_id)
    before = _analytics()

    with SessionLocal() as session:
        session.add(
            _row(
                client_id,
                risk_band="Low",
                route=None,
                signals=_ALL_FALSE,
                balance_tier="Large",
                value_tier="Top",
                recency_band="Recent",
            )
        )
        session.commit()

    after = _analytics()
    assert (
        _count(after, "by_balance_tier", "Large") == _count(before, "by_balance_tier", "Large") + 1
    )
    assert _count(after, "by_value_tier", "Top") == _count(before, "by_value_tier", "Top") + 1
    assert (
        _count(after, "by_recency_band", "Recent")
        == _count(before, "by_recency_band", "Recent") + 1
    )


def test_primary_signal_distribution_reflects_the_one_fired_signal(cleanup) -> None:
    # With exactly one signal fired, it's the primary driver regardless of
    # weight, so the count moves the same way signal_frequency's does.
    client_ids = cleanup
    client_id = 94506
    client_ids.append(client_id)
    signals = dict(_ALL_FALSE)
    signals["sig_never_repeated"] = True
    before = _analytics()

    with SessionLocal() as session:
        session.add(_row(client_id, risk_band="Watch", route=None, signals=signals))
        session.commit()

    after = _analytics()
    assert (
        _count(after, "primary_signal_distribution", "never_repeated")
        == _count(before, "primary_signal_distribution", "never_repeated") + 1
    )
