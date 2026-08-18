"""Tests for GET /risk/analytics/trend: the last N completed nightly runs'
book-wide numbers, oldest first, read off risk_snapshot.

Each test's own run_id is a fresh uuid, so risk_snapshot rows for it are
never touched by any other test -- unlike the current-state cuts in
test_api_risk_analytics.py, these numbers can be asserted exactly rather
than as a delta.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.active_clients import ActiveClientFund
from app.db.models.risk import RiskRun, RiskSnapshot
from app.db.session import SessionLocal
from app.main import app
from app.risk.history import write_snapshot
from app.risk.routing import RouteResult
from app.risk.scoring import ScoreResult

client = TestClient(app)

FUND_ID = 946

SIGNALS = {
    "sig_heavy_withdrawal": False,
    "sig_dormant": True,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
    "sig_never_repeated": False,
}


def _score(
    *, risk_band: str = "Watch", risk_score: int = 40, fund_at_risk: float = 5_000.0
) -> ScoreResult:
    return ScoreResult(
        risk_score=risk_score,
        risk_band=risk_band,
        risk_reasons="No deposit in 12 months",
        fund_at_risk=fund_at_risk,
        signals=SIGNALS,
        recency_band="1-2y",
        balance_tier="Small",
        value_tier="Medium",
    )


def _trend(runs: int = 1) -> list[dict]:
    response = client.get("/api/v1/risk/analytics/trend", params={"runs": runs})
    assert response.status_code == 200
    return response.json()["points"]


def _completed_run(session, run_id: str) -> RiskRun:
    run = RiskRun(run_id=run_id, state="completed", config_version=1)
    session.add(run)
    session.flush()
    run.finished_at = datetime.now(UTC)
    return run


@pytest.fixture
def cleanup():
    client_ids: list[int] = []
    run_ids: list[str] = []
    yield client_ids, run_ids
    with SessionLocal() as session:
        if client_ids:
            session.execute(
                delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids))
            )
        session.execute(delete(RiskSnapshot).where(RiskSnapshot.run_id.in_(run_ids)))
        session.execute(delete(RiskRun).where(RiskRun.run_id.in_(run_ids)))
        session.commit()


def test_shape_is_a_list_of_points() -> None:
    points = _trend(runs=1)
    assert isinstance(points, list)


def test_latest_completed_run_appears_as_the_most_recent_point(cleanup) -> None:
    client_ids, run_ids = cleanup
    client_id = 94601
    client_ids.append(client_id)
    run_id = uuid4().hex
    run_ids.append(run_id)

    with SessionLocal() as session:
        _completed_run(session, run_id)
        write_snapshot(
            session,
            run_id,
            client_id,
            FUND_ID,
            _score(risk_band="Critical", risk_score=90, fund_at_risk=1_234.0),
            RouteResult(route="fa_call_priority", queue_rank=1, complaint_caveat=False),
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        session.commit()

    points = _trend(runs=1)
    assert len(points) == 1
    point = points[0]
    assert point["run_id"] == run_id
    assert point["as_of"] is not None
    assert point["avg_risk_score"] == pytest.approx(90.0)
    assert point["total_fund_at_risk"] == pytest.approx(1_234.0)
    assert point["by_risk_band"] == [{"key": "Critical", "count": 1}]


def test_a_still_running_run_never_appears(cleanup) -> None:
    client_ids, run_ids = cleanup
    completed_id, running_id = 94602, 94603
    client_ids.extend([completed_id, running_id])

    completed_run_id = uuid4().hex
    running_run_id = uuid4().hex
    run_ids.extend([completed_run_id, running_run_id])

    with SessionLocal() as session:
        _completed_run(session, completed_run_id)
        write_snapshot(
            session,
            completed_run_id,
            completed_id,
            FUND_ID,
            _score(),
            RouteResult(route="fa_watchlist", queue_rank=None, complaint_caveat=False),
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        session.add(RiskRun(run_id=running_run_id, state="running", config_version=1))
        session.flush()
        write_snapshot(
            session,
            running_run_id,
            running_id,
            FUND_ID,
            _score(),
            RouteResult(route="fa_watchlist", queue_rank=None, complaint_caveat=False),
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        session.commit()

    points = _trend(runs=1)
    assert len(points) == 1
    assert points[0]["run_id"] == completed_run_id
