"""Reading back a run's events over HTTP: from the start, from the middle,
and for a run that has not said anything yet.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.agent_event import (
    APPROVAL_NEEDED,
    INSIGHT_CREATED,
    RUN_COMPLETED,
    RUN_STARTED,
    RUN_STATUS,
    STEP_COMPLETED,
    STEP_STARTED,
    AgentEvent,
)
from app.db.models.agent_run import AgentRun
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

RUNS = "/api/v1/agent/runs"

STORY = (
    (RUN_STARTED, {"agent": "intelligence", "trigger": "manual"}),
    (STEP_STARTED, {"step": "gather"}),
    (STEP_COMPLETED, {"step": "gather"}),
    (STEP_STARTED, {"step": "investigate"}),
    (RUN_STATUS, {"stage": "investigating", "group_count": 2, "at_once": 2}),
    (RUN_STATUS, {"stage": "group_started", "group_name": "quiet_and_small"}),
    (RUN_STATUS, {"stage": "group_started", "group_name": "everything_else"}),
    (INSIGHT_CREATED, {"insight_id": 71, "group_name": "quiet_and_small", "client_count": 12}),
    (
        RUN_STATUS,
        {
            "stage": "group_done",
            "group_name": "quiet_and_small",
            "outcome": "wrote_findings",
            "insight_count": 1,
        },
    ),
    (
        RUN_STATUS,
        {
            "stage": "group_done",
            "group_name": "everything_else",
            "outcome": "found_nothing",
            "insight_count": 0,
        },
    ),
    (APPROVAL_NEEDED, {"proposal_id": 55, "group_name": "quiet_and_small"}),
    (STEP_COMPLETED, {"step": "investigate"}),
    (RUN_COMPLETED, {"state": "completed", "insight_count": 1}),
)


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def _open_run(session) -> AgentRun:
    run = AgentRun(trigger="manual", state="running", started_at=datetime.now(UTC))
    session.add(run)
    session.commit()
    return run


@pytest.fixture
def quiet_run(db: None):
    """A run that has been opened and has said nothing yet."""
    with SessionLocal() as session:
        run = _open_run(session)
        run_id = run.run_id
    yield run_id
    with SessionLocal() as session:
        session.execute(delete(AgentRun).where(AgentRun.run_id == run_id))
        session.commit()


@pytest.fixture
def told_run(db: None):
    """A finished run with its whole story on file."""
    with SessionLocal() as session:
        run = _open_run(session)
        run_id = run.run_id
        session.add_all(
            AgentEvent(run_id=run_id, ordinal=place, kind=kind, detail=detail)
            for place, (kind, detail) in enumerate(STORY, start=1)
        )
        run.state = "completed"
        session.commit()
    yield run_id
    with SessionLocal() as session:
        session.execute(delete(AgentRun).where(AgentRun.run_id == run_id))
        session.commit()


def test_a_finished_run_reads_back_from_the_start(told_run: int) -> None:
    response = client.get(f"{RUNS}/{told_run}/events")
    assert response.status_code == 200
    body = response.json()

    assert body["run_id"] == told_run
    assert body["state"] == "completed"
    assert [event["ordinal"] for event in body["events"]] == list(range(1, len(STORY) + 1))
    assert body["events"][0]["kind"] == RUN_STARTED
    assert body["events"][-1]["kind"] == RUN_COMPLETED
    assert body["last_ordinal"] == len(STORY)


def test_asking_from_the_middle_returns_only_what_came_after(told_run: int) -> None:
    response = client.get(f"{RUNS}/{told_run}/events", params={"after": 5})
    assert response.status_code == 200
    body = response.json()

    assert [event["ordinal"] for event in body["events"]] == list(range(6, len(STORY) + 1))
    assert body["last_ordinal"] == len(STORY)


def test_asking_from_the_end_returns_nothing_new(told_run: int) -> None:
    body = client.get(f"{RUNS}/{told_run}/events", params={"after": len(STORY)}).json()
    assert body["events"] == []
    assert body["last_ordinal"] == len(STORY)


def test_a_run_that_has_not_started_yet_answers_with_an_empty_story(quiet_run: int) -> None:
    response = client.get(f"{RUNS}/{quiet_run}/events")
    assert response.status_code == 200
    body = response.json()

    assert body["events"] == []
    assert body["last_ordinal"] == 0
    assert body["state"] == "running"
    assert body["progress"]["finished"] is False
    assert body["progress"]["doing_now"] is None
    assert body["progress"]["insights_found"] == 0


def test_the_status_bar_counts_the_same_thing_the_events_say(told_run: int) -> None:
    progress = client.get(f"{RUNS}/{told_run}/events").json()["progress"]

    assert progress["groups_total"] == 2
    assert progress["groups_looked_at"] == 2
    assert progress["insights_found"] == 1
    assert progress["waiting_on_a_person"] == 2
    assert progress["finished"] is True
    assert progress["outcome"] == "completed"
    assert progress["doing_now"] is None


def test_the_status_bar_names_what_a_running_run_is_doing_now(told_run: int) -> None:
    with SessionLocal() as session:
        session.execute(
            delete(AgentEvent).where(AgentEvent.run_id == told_run, AgentEvent.ordinal > 7)
        )
        session.commit()

    progress = client.get(f"{RUNS}/{told_run}/events").json()["progress"]

    assert progress["doing_now"] == "everything_else"
    assert progress["groups_looked_at"] == 0
    assert progress["finished"] is False


def test_the_events_of_an_unknown_run_are_a_404() -> None:
    assert client.get(f"{RUNS}/999999999/events").status_code == 404
