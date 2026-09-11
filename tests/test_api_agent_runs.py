"""The agent runs HTTP API: a person starts a run, reads it back, and lists
runs. Drives the router through TestClient against the real app.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.api.routers import agent_runs as agent_runs_router
from app.db.models.agent_run import NIGHTLY_AGENT, AgentRun
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

RUNS = "/api/v1/agent/runs"


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


@pytest.fixture(autouse=True)
def _no_real_execution(monkeypatch, db: None):
    monkeypatch.setattr(agent_runs_router, "run_agent_in_background", lambda *a, **k: None)
    yield
    with SessionLocal() as session:
        session.execute(
            delete(AgentRun).where(
                AgentRun.trigger == "manual", AgentRun.agent_kind == NIGHTLY_AGENT
            )
        )
        session.commit()


def test_a_person_can_start_a_run() -> None:
    response = client.post(RUNS)
    assert response.status_code == 202
    body = response.json()
    assert body["trigger"] == "manual"
    assert body["state"] == "running"

    with SessionLocal() as session:
        stored = session.get(AgentRun, body["run_id"])
    assert stored is not None
    assert stored.trigger == "manual"


def test_a_second_start_is_refused_while_one_is_running() -> None:
    first = client.post(RUNS)
    assert first.status_code == 202

    second = client.post(RUNS)
    assert second.status_code == 409


def test_reading_back_a_run_by_id() -> None:
    started = client.post(RUNS).json()

    response = client.get(f"{RUNS}/{started['run_id']}")
    assert response.status_code == 200
    assert response.json()["run_id"] == started["run_id"]


def test_reading_an_unknown_run_is_a_404() -> None:
    response = client.get(f"{RUNS}/999999999")
    assert response.status_code == 404


def test_listing_runs_includes_a_started_one() -> None:
    started = client.post(RUNS).json()

    response = client.get(RUNS)
    assert response.status_code == 200
    run_ids = [row["run_id"] for row in response.json()["items"]]
    assert started["run_id"] in run_ids
