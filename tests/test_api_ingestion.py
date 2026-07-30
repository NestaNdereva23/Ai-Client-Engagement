"""The ingestion console API: trigger a pull, and browse run status.

The real Cytonn client is swapped for a fake via FastAPI's own dependency
override, the same pattern the worker's own tests use for paging (a fake
client, an injected payload), just wired through the router instead of
constructed directly.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.routers import ingestion as ingestion_router
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

RUNS = "/api/v1/ingestion/runs"


class FakeClient:
    """Stands in for CytonnClient: no network, one page, injectable liveness."""

    def __init__(self, live: bool = True, payload: dict | None = None):
        self.live = live
        self.payload = payload if payload is not None else {"data": []}
        self.closed = False

    def probe(self, path: str = "") -> bool:
        return self.live

    def fetch(self, path: str = "") -> dict:
        return self.payload

    def close(self) -> None:
        self.closed = True


def _fund_payload(client_id: int) -> dict:
    return {
        "data": [
            {
                "unit_fund_id": 1,
                "unit_fund_name": "Fund",
                "inactive_client_count": 1,
                "clients": [
                    {
                        "client_id": client_id,
                        "client_code": "C1",
                        "client_name": "A Name",
                        "last_5_purchases": [],
                        "last_2_sales": [],
                    }
                ],
            }
        ]
    }


@pytest.fixture
def cleanup_run():
    run_ids: list[str] = []
    yield run_ids
    with SessionLocal() as session:
        for run_id in run_ids:
            session.execute(text("DELETE FROM ingestion_rejects WHERE run_id = :r"), {"r": run_id})
            session.execute(text("DELETE FROM raw_staging WHERE run_id = :r"), {"r": run_id})
            session.execute(text("DELETE FROM ingestion_status WHERE run_id = :r"), {"r": run_id})
        session.commit()


@pytest.fixture
def fake_client():
    fake = FakeClient()
    app.dependency_overrides[ingestion_router.get_cytonn_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(ingestion_router.get_cytonn_client, None)


def test_trigger_run_is_202_and_completes_in_the_background(db, cleanup_run, fake_client) -> None:
    fake_client.payload = _fund_payload(555001)
    response = client.post(RUNS, json={"endpoint": "inactive-clients"})
    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "running"
    run_id = body["run_id"]
    cleanup_run.append(run_id)

    # BackgroundTasks run before TestClient hands control back, so this is
    # already finished, including releasing the client.
    assert fake_client.closed is True

    detail = client.get(f"{RUNS}/{run_id}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["state"] == "completed"
    assert detail_body["records_written"] == 1


def test_trigger_run_resumes_a_given_run_id(db, cleanup_run, fake_client) -> None:
    run_id = uuid4().hex
    cleanup_run.append(run_id)
    with SessionLocal() as session:
        session.execute(
            text(
                "INSERT INTO ingestion_status (run_id, endpoint, state) "
                "VALUES (:r, 'inactive-clients', 'running')"
            ),
            {"r": run_id},
        )
        session.commit()

    fake_client.payload = _fund_payload(555002)
    response = client.post(RUNS, json={"run_id": run_id})
    assert response.status_code == 202
    assert response.json()["run_id"] == run_id


def test_trigger_run_refuses_a_run_id_that_does_not_exist(db, fake_client) -> None:
    """A typo or an unedited API-docs placeholder must never start a fresh run."""
    response = client.post(RUNS, json={"run_id": "string"})
    assert response.status_code == 400
    with SessionLocal() as session:
        row = session.execute(
            text("SELECT 1 FROM ingestion_status WHERE run_id = 'string'")
        ).fetchone()
    assert row is None


def test_trigger_run_without_configured_credentials_is_a_503(monkeypatch) -> None:
    class EmptySettings:
        cytonn_api_base_url = ""
        cytonn_api_key = ""

    monkeypatch.setattr(ingestion_router, "get_settings", lambda: EmptySettings())
    response = client.post(RUNS, json={})
    assert response.status_code == 503


def test_get_run_404s_when_not_found(db: None) -> None:
    response = client.get(f"{RUNS}/not-a-real-run")
    assert response.status_code == 404


def test_list_runs_includes_a_completed_run(db, cleanup_run, fake_client) -> None:
    fake_client.payload = _fund_payload(555003)
    triggered = client.post(RUNS, json={})
    run_id = triggered.json()["run_id"]
    cleanup_run.append(run_id)

    listed = client.get(RUNS)
    assert listed.status_code == 200
    assert run_id in [row["run_id"] for row in listed.json()["items"]]
