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
    monkeypatch.setattr(agent_runs_router, "resume_agent_in_background", lambda *a, **k: None)
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


def _force_state(run_id: int, state: str) -> None:
    with SessionLocal() as session:
        row = session.get(AgentRun, run_id)
        row.state = state
        session.commit()


def test_pausing_a_running_run_flags_it_for_the_loop_to_notice() -> None:
    started = client.post(RUNS).json()
    response = client.post(f"{RUNS}/{started['run_id']}/pause")
    assert response.status_code == 200
    assert response.json()["state"] == "running"
    with SessionLocal() as session:
        row = session.get(AgentRun, started["run_id"])
    assert row.control_requested == "pause"


def test_pausing_a_run_that_is_not_running_is_a_409() -> None:
    started = client.post(RUNS).json()
    _force_state(started["run_id"], "completed")
    response = client.post(f"{RUNS}/{started['run_id']}/pause")
    assert response.status_code == 409


def test_cancelling_a_running_run_flags_it_without_settling_it_yet() -> None:
    started = client.post(RUNS).json()
    response = client.post(f"{RUNS}/{started['run_id']}/cancel")
    assert response.status_code == 200
    assert response.json()["state"] == "running"
    with SessionLocal() as session:
        row = session.get(AgentRun, started["run_id"])
    assert row.control_requested == "cancel"


def test_cancelling_a_paused_run_settles_it_directly() -> None:
    started = client.post(RUNS).json()
    _force_state(started["run_id"], "paused")
    response = client.post(f"{RUNS}/{started['run_id']}/cancel")
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"


def test_resuming_a_paused_run() -> None:
    started = client.post(RUNS).json()
    _force_state(started["run_id"], "paused")
    response = client.post(f"{RUNS}/{started['run_id']}/resume")
    assert response.status_code == 202
    assert response.json()["state"] == "running"


def test_resuming_a_run_that_is_not_paused_is_a_409() -> None:
    started = client.post(RUNS).json()
    response = client.post(f"{RUNS}/{started['run_id']}/resume")
    assert response.status_code == 409


def test_pausing_cancelling_or_resuming_an_unknown_run_is_a_404() -> None:
    assert client.post(f"{RUNS}/999999999/pause").status_code == 404
    assert client.post(f"{RUNS}/999999999/cancel").status_code == 404
    assert client.post(f"{RUNS}/999999999/resume").status_code == 404
