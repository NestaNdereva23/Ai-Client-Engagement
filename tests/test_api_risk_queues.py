"""Tests for GET /risk/queues/dust_cleanup: the read-only ops list, its
cursor pagination, and that it never surfaces a client outside the route.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.risk import ClientRiskFeatures
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

FUND_ID = 941

_SIGNALS = {
    "sig_drawdown": False,
    "sig_dormant": True,
    "sig_cadence_break": False,
    "sig_shrinking": False,
    "sig_fee_erosion": False,
    "sig_never_repeated": False,
}


def _row(client_id: int, route: str, risk_score: int = 10) -> ClientRiskFeatures:
    return ClientRiskFeatures(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance_tier="Dust",
        **_SIGNALS,
        risk_score=risk_score,
        risk_band="Low",
        risk_reasons="No contribution in 12m",
        aum_at_risk=5.0,
        config_version=1,
        route=route,
        queue_rank=None,
    )


@pytest.fixture
def seeded(db):
    client_ids = list(range(94101, 94106))
    with SessionLocal() as session:
        for client_id in client_ids[:-1]:
            session.add(_row(client_id, "dust_cleanup"))
        session.add(_row(client_ids[-1], "monitor_only"))
        session.commit()
    yield client_ids
    with SessionLocal() as session:
        session.execute(
            delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(client_ids))
        )
        session.commit()


def test_returns_only_the_dust_cleanup_population(seeded) -> None:
    response = client.get("/api/v1/risk/queues/dust_cleanup", params={"limit": 200})
    assert response.status_code == 200
    body = response.json()
    ids = {row["client_id"] for row in body["items"]}
    assert ids >= set(seeded[:-1])
    assert seeded[-1] not in ids


def test_every_line_is_actually_routed_dust_cleanup(seeded) -> None:
    response = client.get("/api/v1/risk/queues/dust_cleanup", params={"limit": 200})
    body = response.json()
    for row in body["items"]:
        assert row["risk_band"] is not None
        assert row["aum_at_risk"] is not None


def test_pagination_covers_the_whole_population_with_no_duplicates(seeded) -> None:
    seen: list[int] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get("/api/v1/risk/queues/dust_cleanup", params=params)
        assert response.status_code == 200
        body = response.json()
        seen.extend(row["client_id"] for row in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    expected = set(seeded[:-1])
    assert expected.issubset(set(seen))
    assert len(seen) == len(set(seen))


def test_invalid_cursor_is_a_400(db) -> None:
    response = client.get("/api/v1/risk/queues/dust_cleanup", params={"cursor": "not-a-cursor"})
    assert response.status_code == 400
