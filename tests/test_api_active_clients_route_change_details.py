"""Tests for GET /active-clients/analytics/route-changes/details: the
client-level route moves behind one nightly run's routes_changed count,
read from audit_log's "risk_snapshot"/"route" entry.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.active_clients import ActiveClientFund
from app.db.models.audit import AuditLog
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


URL = "/api/v1/active-clients/analytics/route-changes/details"
FUND_ID = 947


def _make_run_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def seeded():
    run_ids: list[str] = []
    client_ids: list[int] = []
    yield run_ids, client_ids
    with SessionLocal() as session:
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "risk_snapshot",
                AuditLog.action == "route",
                AuditLog.run_id.in_(run_ids),
            )
        )
        session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
        session.commit()


def _seed_run(run_id: str, changed: list[dict]) -> None:
    from app.audit.log import record_audit

    with SessionLocal() as session:
        record_audit(
            session,
            entity_type="risk_snapshot",
            action="route",
            entity_id=run_id,
            run_id=run_id,
            detail={"changed": changed, "changed_count": len(changed)},
        )
        session.commit()


def _seed_fund_row(client_id: int, client_code: str | None = None) -> None:
    with SessionLocal() as session:
        session.add(
            ActiveClientFund(
                client_id=client_id,
                unit_fund_id=FUND_ID,
                client_code=client_code,
                n_deposits=1,
                n_withdrawals=0,
            )
        )
        session.commit()


def test_more_urgent_and_less_urgent_are_labelled(seeded) -> None:
    run_ids, client_ids = seeded
    run_id = _make_run_id()
    run_ids.append(run_id)
    more_urgent_id, less_urgent_id, first_scored_id = 94701, 94702, 94703
    client_ids.extend([more_urgent_id, less_urgent_id, first_scored_id])
    for cid in (more_urgent_id, less_urgent_id, first_scored_id):
        _seed_fund_row(cid, client_code=f"C{cid}")

    _seed_run(
        run_id,
        [
            {
                "client_id": more_urgent_id,
                "unit_fund_id": FUND_ID,
                "from_route": "fa_watchlist",
                "route": "fa_call_priority",
                "from_risk_band": "High",
                "risk_band": "Critical",
                "reasons": "sig_heavy_withdrawal",
            },
            {
                "client_id": less_urgent_id,
                "unit_fund_id": FUND_ID,
                "from_route": "fa_call_priority",
                "route": "monitor_only",
                "from_risk_band": "Critical",
                "risk_band": "Low",
                "reasons": "",
            },
            {
                "client_id": first_scored_id,
                "unit_fund_id": FUND_ID,
                "from_route": None,
                "route": "auto_checkin",
                "risk_band": "Watch",
                "reasons": "sig_shrinking",
            },
        ],
    )

    response = client.get(URL, params={"run_id": run_id})
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["as_of"] is not None
    rows = {row["client_id"]: row for row in body["items"]}

    more_urgent = rows[more_urgent_id]
    assert more_urgent["from_route"] == "fa_watchlist"
    assert more_urgent["to_route"] == "fa_call_priority"
    assert more_urgent["direction"] == "more_urgent"
    assert more_urgent["from_risk_band"] == "High"
    assert more_urgent["client_code"] == f"C{more_urgent_id}"
    assert more_urgent["fund_name"] == f"Fund {FUND_ID}"

    less_urgent = rows[less_urgent_id]
    assert less_urgent["direction"] == "less_urgent"
    assert less_urgent["from_risk_band"] == "Critical"

    first_scored = rows[first_scored_id]
    assert first_scored["from_route"] is None
    assert first_scored["direction"] is None
    assert first_scored["from_risk_band"] is None


def test_defaults_to_the_latest_run_with_changes(seeded) -> None:
    run_ids, client_ids = seeded
    older_run, newer_run = _make_run_id(), _make_run_id()
    run_ids.extend([older_run, newer_run])
    older_client, newer_client = 94704, 94705
    client_ids.extend([older_client, newer_client])
    for cid in (older_client, newer_client):
        _seed_fund_row(cid)

    _seed_run(
        older_run,
        [
            {
                "client_id": older_client,
                "unit_fund_id": FUND_ID,
                "from_route": "monitor_only",
                "route": "auto_checkin",
                "risk_band": "Watch",
                "reasons": "",
            }
        ],
    )
    _seed_run(
        newer_run,
        [
            {
                "client_id": newer_client,
                "unit_fund_id": FUND_ID,
                "from_route": "monitor_only",
                "route": "auto_checkin",
                "risk_band": "Watch",
                "reasons": "",
            }
        ],
    )

    body = client.get(URL).json()
    assert body["run_id"] == newer_run
    assert {row["client_id"] for row in body["items"]} == {newer_client}


def test_pagination_is_capped_at_ten_by_default(seeded) -> None:
    run_ids, client_ids = seeded
    run_id = _make_run_id()
    run_ids.append(run_id)
    ids = list(range(94710, 94722))  # 12 clients
    client_ids.extend(ids)
    for cid in ids:
        _seed_fund_row(cid)

    _seed_run(
        run_id,
        [
            {
                "client_id": cid,
                "unit_fund_id": FUND_ID,
                "from_route": "monitor_only",
                "route": "auto_checkin",
                "risk_band": "Watch",
                "reasons": "",
            }
            for cid in ids
        ],
    )

    first_page = client.get(URL, params={"run_id": run_id}).json()
    assert len(first_page["items"]) == 10
    assert first_page["next_cursor"] is not None

    second_page = client.get(
        URL, params={"run_id": run_id, "cursor": first_page["next_cursor"]}
    ).json()
    assert len(second_page["items"]) == 2
    assert second_page["next_cursor"] is None

    seen = {row["client_id"] for row in first_page["items"]} | {
        row["client_id"] for row in second_page["items"]
    }
    assert seen == set(ids)


def test_a_run_with_no_route_changes_returns_an_empty_page(seeded) -> None:
    run_ids, _client_ids = seeded

    response = client.get(URL, params={"run_id": _make_run_id()})
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["next_cursor"] is None
