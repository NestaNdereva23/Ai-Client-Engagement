"""Watching a run as it happens, over a request that stays open.

The same rows the polling endpoint reads, pushed instead of pulled: from the
start, carried on after a drop, agreeing with polling line for line, tidied
up when the watcher goes away, and capped so many open streams cannot slow
the rest of the API down.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from test_api_agent_events import STORY

from app.api.routers.agent_runs import open_streams
from app.config import get_settings
from app.db.models.agent_event import AgentEvent
from app.db.models.agent_run import AgentRun
from app.db.session import SessionLocal
from app.main import app
from app.services.agent_event_stream import StreamSlots, stream_run_events

client = TestClient(app)

RUNS = "/api/v1/agent/runs"


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


@pytest.fixture(autouse=True)
def _no_streams_left_open():
    open_streams.open_now = 0
    yield
    open_streams.open_now = 0


def _make_run(state: str, events: int) -> int:
    with SessionLocal() as session:
        run = AgentRun(trigger="manual", state="running", started_at=datetime.now(UTC))
        session.add(run)
        session.commit()
        run_id = run.run_id
        session.add_all(
            AgentEvent(run_id=run_id, ordinal=place, kind=kind, detail=detail)
            for place, (kind, detail) in enumerate(STORY[:events], start=1)
        )
        run.state = state
        session.commit()
        return run_id


def _forget(run_id: int) -> None:
    with SessionLocal() as session:
        session.execute(delete(AgentRun).where(AgentRun.run_id == run_id))
        session.commit()


@pytest.fixture
def finished_run(db: None):
    run_id = _make_run("completed", len(STORY))
    yield run_id
    _forget(run_id)


@pytest.fixture
def running_run(db: None):
    run_id = _make_run("running", 3)
    yield run_id
    _forget(run_id)


def read_messages(response) -> list[dict]:
    """Every server sent message in the body, unpacked."""
    messages = []
    current: dict = {}
    for line in response.text.splitlines():
        if line.startswith("id: "):
            current["id"] = int(line[4:])
        elif line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("data: "):
            current["data"] = json.loads(line[6:])
        elif line == "" and current:
            messages.append(current)
            current = {}
    return messages


def activity(messages: list[dict]) -> list[dict]:
    return [m["data"] for m in messages if m["event"] == "activity"]


def test_the_stream_tells_the_whole_story_of_a_finished_run(finished_run: int) -> None:
    with client.stream("GET", f"{RUNS}/{finished_run}/events/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache, no-transform"
        assert response.headers["x-accel-buffering"] == "no"
        response.read()
        messages = read_messages(response)

    assert [event["ordinal"] for event in activity(messages)] == list(range(1, len(STORY) + 1))
    assert messages[-1]["event"] == "end"
    assert messages[-1]["data"]["state"] == "completed"
    assert messages[-1]["data"]["last_ordinal"] == len(STORY)


def test_the_stream_carries_the_same_count_the_status_bar_shows(finished_run: int) -> None:
    with client.stream("GET", f"{RUNS}/{finished_run}/events/stream") as response:
        response.read()
        messages = read_messages(response)

    progress = [m["data"] for m in messages if m["event"] == "progress"][-1]
    polled = client.get(f"{RUNS}/{finished_run}/events").json()["progress"]

    assert progress == polled


def test_a_stream_carries_on_from_where_it_stopped(finished_run: int) -> None:
    with client.stream("GET", f"{RUNS}/{finished_run}/events/stream", params={"after": 5}) as first:
        first.read()
        after_query = activity(read_messages(first))

    headers = {"Last-Event-ID": "5"}
    with client.stream("GET", f"{RUNS}/{finished_run}/events/stream", headers=headers) as second:
        second.read()
        after_header = activity(read_messages(second))

    assert [event["ordinal"] for event in after_query] == list(range(6, len(STORY) + 1))
    assert after_header == after_query


def test_a_resumed_stream_never_repeats_or_skips_an_event(finished_run: int) -> None:
    with client.stream("GET", f"{RUNS}/{finished_run}/events/stream", params={"after": 0}) as first:
        first.read()
        seen = activity(read_messages(first))[:4]

    stopped_at = seen[-1]["ordinal"]
    resume = {"after": stopped_at}
    with client.stream("GET", f"{RUNS}/{finished_run}/events/stream", params=resume) as second:
        second.read()
        rest = activity(read_messages(second))

    ordinals = [event["ordinal"] for event in seen] + [event["ordinal"] for event in rest]
    assert ordinals == list(range(1, len(STORY) + 1))


def test_the_stream_shows_what_polling_shows(finished_run: int) -> None:
    with client.stream("GET", f"{RUNS}/{finished_run}/events/stream") as response:
        response.read()
        streamed = activity(read_messages(response))

    polled = client.get(f"{RUNS}/{finished_run}/events").json()["events"]

    assert [event["ordinal"] for event in streamed] == [event["ordinal"] for event in polled]
    assert [event["kind"] for event in streamed] == [event["kind"] for event in polled]
    assert [event["detail"] for event in streamed] == [event["detail"] for event in polled]


async def test_a_watcher_that_goes_away_leaves_nothing_behind(running_run: int) -> None:
    slots = StreamSlots(limit=1)
    assert slots.take()
    stream = stream_run_events(running_run, after=0, settings=get_settings(), slots=slots)

    assert await anext(stream)
    assert slots.open_now == 1

    await stream.aclose()
    assert slots.open_now == 0


def test_a_stream_ends_by_itself_when_the_run_ends(running_run: int) -> None:
    _finish_shortly(running_run, seconds=1.0)

    with client.stream("GET", f"{RUNS}/{running_run}/events/stream") as response:
        response.read()
        messages = read_messages(response)

    assert messages[-1]["event"] == "end"
    assert messages[-1]["data"]["state"] == "completed"
    _wait_until(lambda: open_streams.open_now == 0)


def test_only_so_many_people_may_watch_at_once(running_run: int, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_stream_max_open", 2)
    watchers = [threading.Thread(target=_read_a_stream, args=(running_run,)) for _ in range(2)]

    for watcher in watchers:
        watcher.start()
    try:
        _wait_until(lambda: open_streams.open_now == 2)
        assert client.get(f"{RUNS}/{running_run}/events/stream").status_code == 503

        started = time.monotonic()
        assert client.get("/health").status_code == 200
        assert time.monotonic() - started < 2.0
    finally:
        _finish_now(running_run)
        for watcher in watchers:
            watcher.join(timeout=15)

    assert open_streams.open_now == 0


def test_the_stream_of_an_unknown_run_is_a_404(db: None) -> None:
    assert client.get(f"{RUNS}/999999999/events/stream").status_code == 404
    assert open_streams.open_now == 0


def _read_a_stream(run_id: int) -> None:
    """Hold a stream open until the run it is watching ends."""
    watcher = TestClient(app, headers=dict(client.headers))
    watcher.get(f"{RUNS}/{run_id}/events/stream")


def _finish_now(run_id: int) -> None:
    with SessionLocal() as session:
        run = session.get(AgentRun, run_id)
        run.state = "completed"
        session.commit()


def _finish_shortly(run_id: int, seconds: float) -> None:
    threading.Timer(seconds, _finish_now, args=(run_id,)).start()


def _wait_until(ready, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(0.05)
