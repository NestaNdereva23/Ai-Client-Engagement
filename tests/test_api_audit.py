"""The audit and trace console API: browse the append-only log, deep-link a trace."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.audit.log import record_audit
from app.config import Settings
from app.db.models.audit import AuditLog
from app.db.models.llmops import GenerationRun, TraceRef
from app.db.models.models import Clients, Funds
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.main import app

client = TestClient(app)

AUDIT = "/api/v1/audit"
TRACES = "/api/v1/traces"


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def test_missing_token_is_401(configured_reviewers) -> None:
    response = TestClient(app).get(AUDIT)
    assert response.status_code == 401


def test_no_reviewer_configured_is_503(unconfigured_reviewers, reviewer_1_headers) -> None:
    response = TestClient(app).get(AUDIT, headers=reviewer_1_headers)
    assert response.status_code == 503


@pytest.fixture
def an_audit_row(db: None):
    run_id = uuid4().hex
    trace_id = uuid4().hex
    with SessionLocal() as session:
        record_audit(
            session,
            entity_type="test_entity",
            action="test_action",
            entity_id="e1",
            run_id=run_id,
            trace_id=trace_id,
            detail={"k": "v"},
        )
        session.commit()

    yield run_id, trace_id

    with SessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.run_id == run_id))
        session.commit()


def test_list_audit_log_filters_by_entity(an_audit_row) -> None:
    run_id, _trace_id = an_audit_row
    response = client.get(AUDIT, params={"entity": "test_entity", "run": run_id})
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["action"] == "test_action"
    assert items[0]["detail"] == {"k": "v"}


def test_list_audit_log_filters_by_trace(an_audit_row) -> None:
    _run_id, trace_id = an_audit_row
    response = client.get(AUDIT, params={"trace": trace_id})
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["trace_id"] == trace_id


def test_list_audit_log_excludes_unrelated_rows(an_audit_row) -> None:
    response = client.get(AUDIT, params={"run": "not-a-real-run"})
    assert response.json()["items"] == []


def _make_settings() -> Settings:
    return Settings(
        llm_provider="anthropic",
        anthropic_api_key="test-key",
        llm_model="claude-opus-5",
        llm_temperature=None,
        llm_max_tokens=1024,
    )


@pytest.fixture
def a_trace(db: None):
    fund_id = 975
    client_id = 97501
    trace_id = uuid4().hex
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.commit()
        run = persist_generation_run(
            session,
            {
                "run_id": uuid4().hex,
                "trace_id": trace_id,
                "client_id": client_id,
                "status": "accepted",
                "attempts": 1,
                "raw_structured_output": {"subject": "s", "body": "b"},
            },
            _make_settings(),
        )
        session.add(
            TraceRef(
                run_id=run.run_id,
                trace_id=trace_id,
                trace_url=f"https://langfuse.example/t/{trace_id}",
            )
        )
        session.commit()
        run_id = run.run_id

    yield trace_id

    with SessionLocal() as session:
        session.execute(delete(TraceRef).where(TraceRef.trace_id == trace_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_get_trace_returns_the_url(a_trace) -> None:
    response = client.get(f"{TRACES}/{a_trace}")
    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"] == a_trace
    assert body["trace_url"].endswith(a_trace)


def test_get_trace_404s_when_not_found(db: None) -> None:
    response = client.get(f"{TRACES}/not-a-real-trace")
    assert response.status_code == 404
