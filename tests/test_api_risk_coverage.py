"""Tests for GET /risk/coverage: book_size vs. how many of them the last
completed nightly run actually scored.

Both counts are read as deltas against a baseline taken before each test
seeds its own rows, so the numbers stay correct no matter what other data
already lives in the shared test database.
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

FUND_ID = 944


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def test_missing_token_is_401(configured_reviewers) -> None:
    response = TestClient(app).get("/api/v1/risk/coverage")
    assert response.status_code == 401


def test_no_reviewer_configured_is_503(unconfigured_reviewers, reviewer_1_headers) -> None:
    response = TestClient(app).get("/api/v1/risk/coverage", headers=reviewer_1_headers)
    assert response.status_code == 503


SIGNALS = {
    "sig_heavy_withdrawal": False,
    "sig_dormant": True,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
    "sig_never_repeated": False,
}


def _score() -> ScoreResult:
    return ScoreResult(
        risk_score=40,
        risk_band="Watch",
        risk_reasons="No deposit in 12 months",
        fund_at_risk=5_000.0,
        signals=SIGNALS,
        recency_band="1-2y",
        balance_tier="Small",
        value_tier="Medium",
    )


def _coverage() -> dict:
    response = client.get("/api/v1/risk/coverage")
    assert response.status_code == 200
    return response.json()


def _completed_run(session, run_id: str) -> RiskRun:
    """A completed RiskRun with finished_at set, the same invariant
    risk_detection.py's own completion step keeps.
    """
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


def test_book_size_counts_every_active_client_fund_row(db, cleanup) -> None:
    client_ids, _ = cleanup
    ids = [94401, 94402]
    client_ids.extend(ids)

    before = _coverage()
    with SessionLocal() as session:
        for client_id in ids:
            session.add(
                ActiveClientFund(
                    client_id=client_id, unit_fund_id=FUND_ID, n_deposits=1, n_withdrawals=0
                )
            )
        session.commit()

    after = _coverage()
    assert after["book_size"] == before["book_size"] + len(ids)


def test_scored_count_is_the_latest_completed_runs_snapshot_count(db, cleanup) -> None:
    client_ids, run_ids = cleanup
    ids = [94403, 94404, 94405]
    client_ids.extend(ids)

    run_id = uuid4().hex
    run_ids.append(run_id)
    with SessionLocal() as session:
        _completed_run(session, run_id)
        for client_id in ids:
            write_snapshot(
                session,
                run_id,
                client_id,
                FUND_ID,
                _score(),
                RouteResult(route="fa_watchlist", queue_rank=None, complaint_caveat=False),
                config_version=1,
                pattern_is_reliable=True,
                overdue_multiple=1.0,
            )
        session.commit()

    body = _coverage()
    assert body["scored_count"] == len(ids)
    assert body["as_of"] is not None


def test_a_still_running_run_is_never_the_latest(db, cleanup) -> None:
    client_ids, run_ids = cleanup
    completed_id, running_id = 94406, 94407
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

    body = _coverage()
    # Only the completed run's one snapshot counts; the running run's
    # snapshot must never be picked up even though it was written more
    # recently in wall-clock time.
    assert body["scored_count"] == 1


def test_shape_is_always_present_even_with_no_completed_run() -> None:
    body = _coverage()
    assert isinstance(body["book_size"], int)
    assert isinstance(body["scored_count"], int)
    assert "as_of" in body
