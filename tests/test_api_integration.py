from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from app.api.routers import ingestion as ingestion_router
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.suppression import Suppression
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

CONTACTS = "/api/v1/integration/contacts"
SUPPRESSIONS = "/api/v1/integration/suppressions"
TRIGGER = "/api/v1/integration/ingestion/runs"


def _role_present() -> bool:
    with SessionLocal() as session:
        return bool(session.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'ace_restricted'")))


@pytest.fixture
def roles(db: None):
    if not _role_present():
        pytest.skip("boundary roles not present; run alembic upgrade head")


def test_missing_token_is_401(configured_reviewers) -> None:
    response = client.post(CONTACTS, json={"client_id": 1})
    assert response.status_code == 401


def test_bad_token_is_401(configured_reviewers) -> None:
    response = client.post(
        CONTACTS, json={"client_id": 1}, headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert response.status_code == 401


def test_no_secret_configured_is_503(unconfigured_reviewers, reviewer_1_headers) -> None:
    response = client.post(CONTACTS, json={"client_id": 1}, headers=reviewer_1_headers)
    assert response.status_code == 503


@pytest.fixture
def a_client(roles):
    fund_id = 976
    client_id = 97601
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                client_code="CODE-976",
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.commit()

    yield client_id

    with SessionLocal() as session:
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_upsert_contact_by_client_id(configured_reviewers, reviewer_1_headers, a_client) -> None:
    response = client.post(
        CONTACTS,
        json={
            "client_id": a_client,
            "contact_email": "jane@example.com",
            "consent": True,
        },
        headers=reviewer_1_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["client_id"] == a_client
    assert body["contact_email"] == "jane@example.com"
    assert body["consent"] is True


def test_upsert_contact_by_client_code(configured_reviewers, reviewer_1_headers, a_client) -> None:
    response = client.post(
        CONTACTS,
        json={"client_code": "CODE-976", "contact_whatsapp": "+254700000000", "consent": True},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 200
    assert response.json()["client_id"] == a_client


def test_upsert_contact_with_unknown_code_is_404(
    configured_reviewers, reviewer_1_headers, roles
) -> None:
    response = client.post(
        CONTACTS, json={"client_code": "no-such-code", "consent": True}, headers=reviewer_1_headers
    )
    assert response.status_code == 404


def test_upsert_contact_requires_an_identifier(configured_reviewers, reviewer_1_headers) -> None:
    response = client.post(CONTACTS, json={"consent": True}, headers=reviewer_1_headers)
    assert response.status_code == 422


def test_a_second_upsert_does_not_blank_out_an_earlier_field(
    configured_reviewers, reviewer_1_headers, a_client
) -> None:
    client.post(
        CONTACTS,
        json={"client_id": a_client, "contact_email": "jane@example.com", "consent": True},
        headers=reviewer_1_headers,
    )
    response = client.post(
        CONTACTS, json={"client_id": a_client, "consent": False}, headers=reviewer_1_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["contact_email"] == "jane@example.com"
    assert body["consent"] is False


@pytest.fixture
def cleanup_suppression(roles):
    client_ids: list[int] = []
    yield client_ids
    with SessionLocal() as session:
        session.execute(delete(Suppression).where(Suppression.client_id.in_(client_ids)))
        session.commit()


def test_add_suppression_creates_a_row(
    configured_reviewers, reviewer_1_headers, cleanup_suppression
) -> None:
    cleanup_suppression.append(555555)
    response = client.post(
        SUPPRESSIONS,
        json={"client_id": 555555, "reason": "unsubscribe", "source": "cytonn-crm"},
        headers=reviewer_1_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["client_id"] == 555555
    assert body["reason"] == "unsubscribe"

    with SessionLocal() as session:
        row = session.get(Suppression, 555555)
    assert row is not None
    assert row.source == "cytonn-crm"


def test_resyncing_a_suppression_updates_the_reason(
    configured_reviewers, reviewer_1_headers, cleanup_suppression
) -> None:
    cleanup_suppression.append(555556)
    client.post(
        SUPPRESSIONS,
        json={"client_id": 555556, "reason": "unsubscribe"},
        headers=reviewer_1_headers,
    )
    response = client.post(
        SUPPRESSIONS, json={"client_id": 555556, "reason": "opt_out"}, headers=reviewer_1_headers
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "opt_out"

    with SessionLocal() as session:
        count = session.scalar(
            select(func.count()).select_from(Suppression).where(Suppression.client_id == 555556)
        )
    assert count == 1


class FakeClient:
    def __init__(self, payload: dict | None = None):
        self.payload = payload if payload is not None else {"data": []}
        self.closed = False

    def probe(self, path: str = "") -> bool:
        return True

    def fetch(self, path: str = "", *, params: dict | None = None) -> dict:
        return self.payload

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_client():
    fake = FakeClient()
    app.dependency_overrides[ingestion_router.get_cytonn_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(ingestion_router.get_cytonn_client, None)


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


def test_trigger_via_integration_plane_completes(
    db, configured_reviewers, reviewer_1_headers, cleanup_run, fake_client
) -> None:
    response = client.post(TRIGGER, json={}, headers=reviewer_1_headers)
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    cleanup_run.append(run_id)
    assert fake_client.closed is True

    status = client.get(f"/api/v1/ingestion/runs/{run_id}", headers=reviewer_1_headers)
    assert status.json()["state"] == "completed"


def test_trigger_via_integration_plane_without_a_token_is_401(
    db, configured_reviewers, fake_client
) -> None:
    response = client.post(TRIGGER, json={})
    assert response.status_code == 401
