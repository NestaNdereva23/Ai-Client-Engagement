"""Tests for GET /active-clients/analytics/route-changes: route-churn
history read off audit_log's "risk_run"/"complete" entries, newest first.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.audit import AuditLog
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


URL = "/api/v1/active-clients/analytics/route-changes"


def _make_run_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def seeded_runs():
    run_ids: list[str] = []
    yield run_ids
    with SessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.run_id.in_(run_ids)))
        session.commit()


def _seed_run(run_id: str, *, clients_seen: int, routes_changed: int, route_distribution: dict):
    from app.audit.log import record_audit

    with SessionLocal() as session:
        record_audit(
            session,
            entity_type="risk_run",
            action="complete",
            entity_id=run_id,
            run_id=run_id,
            detail={
                "clients_seen": clients_seen,
                "route_distribution": route_distribution,
                "routes_changed": routes_changed,
            },
        )
        session.commit()


def test_shape_includes_run_id_and_counts(seeded_runs) -> None:
    run_id = _make_run_id()
    seeded_runs.append(run_id)
    _seed_run(
        run_id,
        clients_seen=5,
        routes_changed=3,
        route_distribution={"fa_call_priority": 2, "small_balance_review": 1},
    )

    response = client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["items"], list)

    row = next(item for item in body["items"] if item["run_id"] == run_id)
    assert row["clients_seen"] == 5
    assert row["routes_changed"] == 3
    assert row["route_distribution"] == {"fa_call_priority": 2, "small_balance_review": 1}
    assert row["as_of"] is not None


def test_newest_run_first(seeded_runs) -> None:
    older_run = _make_run_id()
    newer_run = _make_run_id()
    seeded_runs.extend([older_run, newer_run])
    _seed_run(older_run, clients_seen=1, routes_changed=1, route_distribution={})
    _seed_run(newer_run, clients_seen=2, routes_changed=2, route_distribution={})

    body = client.get(URL).json()
    run_ids = [item["run_id"] for item in body["items"]]
    assert run_ids.index(newer_run) < run_ids.index(older_run)


def test_pagination_limit_is_respected(seeded_runs) -> None:
    run_a = _make_run_id()
    run_b = _make_run_id()
    seeded_runs.extend([run_a, run_b])
    _seed_run(run_a, clients_seen=1, routes_changed=0, route_distribution={})
    _seed_run(run_b, clients_seen=1, routes_changed=0, route_distribution={})

    response = client.get(URL, params={"limit": 1})
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["next_cursor"] is not None


def test_a_run_with_no_detail_defaults_to_zero(seeded_runs) -> None:
    run_id = _make_run_id()
    seeded_runs.append(run_id)

    from app.audit.log import record_audit

    with SessionLocal() as session:
        record_audit(
            session,
            entity_type="risk_run",
            action="complete",
            entity_id=run_id,
            run_id=run_id,
            detail=None,
        )
        session.commit()

    body = client.get(URL).json()
    row = next(item for item in body["items"] if item["run_id"] == run_id)
    assert row["clients_seen"] == 0
    assert row["routes_changed"] == 0
    assert row["route_distribution"] == {}
    assert row["more_urgent_count"] == 0
    assert row["less_urgent_count"] == 0


def test_more_urgent_and_less_urgent_counts_come_from_the_route_audit_entry(seeded_runs) -> None:
    """more_urgent_count/less_urgent_count are read from the same
    "risk_snapshot"/"route" entry the route-change-details endpoint reads,
    not from this entry's own detail -- so they still show up correctly
    even though this "risk_run"/"complete" entry never carried them.
    """
    run_id = _make_run_id()
    seeded_runs.append(run_id)
    _seed_run(run_id, clients_seen=3, routes_changed=3, route_distribution={})

    from app.audit.log import record_audit

    with SessionLocal() as session:
        record_audit(
            session,
            entity_type="risk_snapshot",
            action="route",
            entity_id=run_id,
            run_id=run_id,
            detail={
                "changed": [
                    {
                        "client_id": 1,
                        "unit_fund_id": 1,
                        "from_route": "fa_watchlist",
                        "route": "fa_call_priority",
                        "risk_band": "Critical",
                        "reasons": "",
                    },
                    {
                        "client_id": 2,
                        "unit_fund_id": 1,
                        "from_route": "fa_call_priority",
                        "route": "monitor_only",
                        "risk_band": "Low",
                        "reasons": "",
                    },
                    {
                        "client_id": 3,
                        "unit_fund_id": 1,
                        "from_route": None,
                        "route": "auto_checkin",
                        "risk_band": "Watch",
                        "reasons": "",
                    },
                ],
                "changed_count": 3,
            },
        )
        session.commit()

    body = client.get(URL).json()
    row = next(item for item in body["items"] if item["run_id"] == run_id)
    assert row["more_urgent_count"] == 1
    assert row["less_urgent_count"] == 1
