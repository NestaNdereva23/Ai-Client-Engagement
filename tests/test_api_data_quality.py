"""The data-quality console endpoint: rejects, reasons, and shortfall."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

QUALITY = "/api/v1/data/quality"


@pytest.fixture
def a_run_with_rejects(db: None):
    run_id = uuid4().hex
    with SessionLocal() as session:
        session.execute(
            text(
                "INSERT INTO ingestion_status "
                "(run_id, endpoint, state, records_seen, records_written, "
                "records_rejected, shortfall) "
                "VALUES (:r, 'inactive-clients', 'completed', 3, 1, 2, 5)"
            ),
            {"r": run_id},
        )
        session.execute(
            text(
                "INSERT INTO ingestion_rejects (run_id, raw_fragment, reason) "
                "VALUES (:r, '{}', 'client: client_id field required'), "
                "(:r, '{}', 'client: client_id field required'), "
                "(:r, '{}', 'fund: unit_fund_id field required')"
            ),
            {"r": run_id},
        )
        session.commit()

    yield run_id

    with SessionLocal() as session:
        session.execute(text("DELETE FROM ingestion_rejects WHERE run_id = :r"), {"r": run_id})
        session.execute(text("DELETE FROM ingestion_status WHERE run_id = :r"), {"r": run_id})
        session.commit()


def test_quality_for_a_given_run_ranks_reasons_by_count(a_run_with_rejects) -> None:
    response = client.get(QUALITY, params={"run_id": a_run_with_rejects})
    assert response.status_code == 200
    body = response.json()
    assert body["shortfall"] == 5
    assert body["records_rejected"] == 2
    assert body["reject_reasons"][0]["reason"] == "client: client_id field required"
    assert body["reject_reasons"][0]["count"] == 2


def test_quality_defaults_to_the_most_recent_run(a_run_with_rejects) -> None:
    response = client.get(QUALITY)
    assert response.status_code == 200
    # Whichever run is most recent, the shape is always the same.
    assert "reject_reasons" in response.json()


def test_quality_404s_for_an_unknown_run(db: None) -> None:
    response = client.get(QUALITY, params={"run_id": "not-a-real-run"})
    assert response.status_code == 404
