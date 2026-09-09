"""The ordered record of what happened during a run.

These cover what the record has to get right: a finished run replays from
the table alone in the order it happened, groups worked side by side still
leave one clean order, and a run that fails says so before it stops. The
rest hold the two promises the record makes to the run itself, that writing
an event never fails it and never carries a name out with it.
"""

from __future__ import annotations

import threading
from datetime import date

import pytest
from sqlalchemy import delete, select

from app.agents import intelligence as intelligence_module
from app.agents.events import NO_EVENTS, RunEventLog, stored_detail
from app.agents.insight_state import transition_insight
from app.agents.intelligence import run_intelligence_agent
from app.agents.watchlist import FEES_WILL_EMPTY, VERY_SMALL_AND_QUIET, WatchlistThresholds
from app.db.async_session import dispose_async_engine
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_event import (
    AGENT_EVENT_KINDS,
    ERROR,
    INSIGHT_CREATED,
    INSIGHT_DISMISSED,
    RUN_COMPLETED,
    RUN_STARTED,
    RUN_STATUS,
    STEP_COMPLETED,
    STEP_STARTED,
    TOOL_COMPLETED,
    TOOL_STARTED,
    AgentEvent,
)
from app.db.models.agent_insight import AgentInsight, AgentInsightFact
from app.db.models.agent_run import AgentRun, AgentToolCall
from app.db.models.audit import AuditLog
from app.db.models.risk import ClientRiskFeatures
from app.db.session import SessionLocal
from app.privacy.llm_client import ConversationTurn, LLMUsage, ToolUseRequest

FUND_ID = 9841
CLIENT_IDS = tuple(range(984101, 984105))

AS_OF = date(2026, 9, 8)

THRESHOLDS = WatchlistThresholds(
    new_client_days=30,
    months_until_empty=6.0,
    small_balance=1_000.0,
    awaiting_call_days=2,
)

_USAGE = LLMUsage(input_tokens=10, output_tokens=10)

_GROUP_MARKER = "Tonight you are looking at one group: "

EXPECTED_KINDS = (
    "run_started",
    "run_status",
    "run_completed",
    "step_started",
    "step_completed",
    "tool_started",
    "tool_completed",
    "insight_created",
    "insight_updated",
    "insight_dismissed",
    "proposal_created",
    "approval_needed",
    "approval_given",
    "action_started",
    "action_completed",
    "warning",
    "error",
    "paused",
    "resumed",
)


class ScriptedAsyncClient:
    """Answers each group's investigation from its own list of turns."""

    model = "fake-intelligence-model"

    def __init__(self, scripts: dict[str, list]) -> None:
        self._scripts = {name: list(turns) for name, turns in scripts.items()}

    async def aconverse(self, *, system, messages, tools=()):
        group_name = system.split(_GROUP_MARKER, 1)[1].split(".", 1)[0]
        queue = self._scripts.get(group_name)
        if queue is None:
            return _final_answer("Nothing to add for this group.")
        if not queue:
            raise AssertionError(f"the fake model ran out of turns for {group_name}")
        return queue.pop(0)


class FakeSession:
    """A session that counts what it was asked to do and can refuse to commit."""

    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.written: list[AgentEvent] = []
        self.rollbacks = 0
        self.closed = False

    def add_all(self, rows) -> None:
        self.written.extend(rows)

    def commit(self) -> None:
        if self.fails:
            raise RuntimeError("the database is not there")

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _final_answer(text: str) -> ConversationTurn:
    return ConversationTurn(text=text, tool_requests=(), usage=_USAGE, stop_reason="end_turn")


def _calls(*requests: tuple[str, dict]) -> ConversationTurn:
    return ConversationTurn(
        text="",
        tool_requests=tuple(
            ToolUseRequest(call_id=f"call_{index}", tool_name=name, tool_input=arguments)
            for index, (name, arguments) in enumerate(requests)
        ),
        usage=_USAGE,
        stop_reason="tool_use",
    )


def _write(title: str, group_name: str, **overrides) -> tuple[str, dict]:
    arguments = {
        "kind": "risk",
        "title": title,
        "group_name": group_name,
        "client_count": 4,
        "suggestion": "Ask a person to look at these before the balance runs out.",
        "why_now": "The fee will empty these accounts within a few months.",
        "confidence": "medium",
        "confidence_reason": "The counts are small but they all point the same way.",
    }
    arguments.update(overrides)
    return ("write_insight", arguments)


def _dismiss(group_name: str, reason: str) -> tuple[str, dict]:
    return ("dismiss_group", {"group_name": group_name, "reason": reason})


def _purge(session) -> None:
    run_ids = session.scalars(select(AgentRun.run_id).where(AgentRun.trigger == "manual")).all()
    insight_ids = session.scalars(
        select(AgentInsight.insight_id).where(AgentInsight.run_id.in_(run_ids))
    ).all()
    if insight_ids:
        session.execute(
            delete(AgentInsightFact).where(AgentInsightFact.insight_id.in_(insight_ids))
        )
        session.execute(delete(AgentInsight).where(AgentInsight.insight_id.in_(insight_ids)))
    if run_ids:
        session.execute(delete(AuditLog).where(AuditLog.run_id.in_([str(r) for r in run_ids])))
        session.execute(delete(AgentToolCall).where(AgentToolCall.run_id.in_(run_ids)))
        session.execute(delete(AgentRun).where(AgentRun.run_id.in_(run_ids)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.unit_fund_id == FUND_ID))
    session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.unit_fund_id == FUND_ID))
    session.commit()


@pytest.fixture(autouse=True)
async def _dispose_async_engine_after_each_test():
    yield
    await dispose_async_engine()


@pytest.fixture
def clean(db: None):
    with SessionLocal() as session:
        _purge(session)
    yield
    with SessionLocal() as session:
        _purge(session)


@pytest.fixture(autouse=True)
def _fixed_thresholds(monkeypatch, clean: None):
    monkeypatch.setattr(intelligence_module, "load_thresholds", lambda session, as_of: THRESHOLDS)


@pytest.fixture
def book(clean: None):
    """A handful of client funds the fee will empty, half of them tiny."""
    with SessionLocal() as session:
        for index, client_id in enumerate(CLIENT_IDS):
            session.add(
                ActiveClientFund(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    balance=600.0 if index % 2 else 200_000.0,
                    n_deposits=4,
                    n_withdrawals=0,
                    months_until_empty=2.0,
                )
            )
            session.add(
                ClientRiskFeatures(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    risk_band="High",
                    value_tier="Silver",
                    balance_tier="Small",
                    recency_band="Lapsed",
                    pattern_is_reliable=True,
                    overdue_multiple=2.0,
                    sig_heavy_withdrawal=False,
                    sig_dormant=index % 2 == 1,
                    sig_broken_pattern=False,
                    sig_shrinking=False,
                    sig_going_dormant=False,
                    sig_never_repeated=False,
                    risk_score=70,
                    risk_reasons="dormant",
                    fund_at_risk=1_000.0,
                    config_version=1,
                )
            )
        session.commit()


async def _run(scripts: dict[str, list], **kwargs) -> int:
    with SessionLocal() as session:
        run = await run_intelligence_agent(
            session,
            trigger="manual",
            llm_client=ScriptedAsyncClient(scripts),
            as_of=AS_OF,
            **kwargs,
        )
        return run.run_id


def _events(run_id: int) -> list[AgentEvent]:
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run_id).order_by(AgentEvent.ordinal)
            ).all()
        )


def test_the_table_allows_exactly_the_kinds_that_were_asked_for() -> None:
    assert AGENT_EVENT_KINDS == EXPECTED_KINDS
    assert len(set(AGENT_EVENT_KINDS)) == len(AGENT_EVENT_KINDS)


def test_events_written_from_many_threads_never_claim_the_same_place() -> None:
    session = FakeSession()
    log = RunEventLog(1, session_factory=lambda: session)
    ready = threading.Barrier(8)

    def shout() -> None:
        ready.wait()
        for _ in range(100):
            log.record(RUN_STATUS, stage="group_done")

    threads = [threading.Thread(target=shout) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    log.close()

    ordinals = sorted(row.ordinal for row in session.written)
    assert ordinals == list(range(1, 801))
    assert session.closed


def test_a_write_that_fails_never_reaches_the_run() -> None:
    session = FakeSession(fails=True)
    log = RunEventLog(1, session_factory=lambda: session)

    log.record(RUN_STARTED, agent="intelligence")
    log.record(RUN_COMPLETED, state="completed")
    log.close()

    assert session.rollbacks >= 1
    assert session.closed


def test_the_log_that_throws_events_away_accepts_anything() -> None:
    NO_EVENTS.record(RUN_STARTED, agent="intelligence")
    NO_EVENTS.close()


def test_a_detail_carrying_a_contact_channel_is_withheld() -> None:
    kept = stored_detail(RUN_STATUS, {"group_name": FEES_WILL_EMPTY, "client_count": 4})
    assert kept == {"group_name": FEES_WILL_EMPTY, "client_count": 4}

    withheld = stored_detail(RUN_STATUS, {"note": "write to grace.wanjiru@example.com"})
    assert withheld == {"withheld": True}


async def test_a_finished_run_replays_from_the_event_table_alone(book: None) -> None:
    scripts = {
        FEES_WILL_EMPTY: [
            _calls(_write("The fee will empty these accounts", FEES_WILL_EMPTY)),
            _final_answer("One finding written."),
        ],
        VERY_SMALL_AND_QUIET: [
            _calls(_dismiss(VERY_SMALL_AND_QUIET, "These are too small to be worth chasing.")),
            _final_answer("Nothing here."),
        ],
    }

    run_id = await _run(scripts, concurrency=1)
    events = _events(run_id)
    kinds = [row.kind for row in events]

    assert [row.ordinal for row in events] == list(range(1, len(events) + 1))
    assert kinds[0] == RUN_STARTED
    assert kinds[-1] == RUN_COMPLETED
    assert events[-1].detail["state"] == "completed"

    steps_started = [row.detail["step"] for row in events if row.kind == STEP_STARTED]
    steps_completed = [row.detail["step"] for row in events if row.kind == STEP_COMPLETED]
    assert steps_started == ["gather", "investigate", "record", "summarise"]
    assert steps_completed == steps_started

    created = [row for row in events if row.kind == INSIGHT_CREATED]
    assert len(created) == 1
    assert created[0].detail["group_name"] == FEES_WILL_EMPTY

    assert kinds.count(TOOL_STARTED) == kinds.count(TOOL_COMPLETED) == 2
    assert ERROR not in kinds


async def test_groups_worked_side_by_side_still_leave_one_clean_order(book: None) -> None:
    scripts = {
        FEES_WILL_EMPTY: [
            _calls(_write("The fee will empty these accounts", FEES_WILL_EMPTY)),
            _final_answer("One finding written."),
        ],
        VERY_SMALL_AND_QUIET: [
            _calls(_dismiss(VERY_SMALL_AND_QUIET, "These are too small to be worth chasing.")),
            _final_answer("Nothing here."),
        ],
    }

    run_id = await _run(scripts, concurrency=4)
    events = _events(run_id)

    ordinals = [row.ordinal for row in events]
    assert ordinals == sorted(set(ordinals))
    assert ordinals == list(range(1, len(events) + 1))

    started = {
        row.detail["group_name"]: row.ordinal
        for row in events
        if row.kind == RUN_STATUS and row.detail.get("stage") == "group_started"
    }
    done = {
        row.detail["group_name"]: row.ordinal
        for row in events
        if row.kind == RUN_STATUS and row.detail.get("stage") == "group_done"
    }
    assert set(started) == set(done)
    assert all(started[name] < done[name] for name in started)


async def test_a_failed_run_writes_the_error_before_it_stops(book: None, monkeypatch) -> None:
    def explode(session, as_of):
        raise RuntimeError("the watch list could not be built")

    monkeypatch.setattr(intelligence_module, "gather_context", explode)

    run_id = await _run({})
    events = _events(run_id)
    kinds = [row.kind for row in events]

    assert ERROR in kinds
    assert kinds[-1] == RUN_COMPLETED
    assert events[-1].detail["state"] == "failed"
    assert kinds.index(ERROR) < len(kinds) - 1

    failure = next(row for row in events if row.kind == ERROR)
    assert failure.detail["about"] == "step"
    assert "watch list" in failure.detail["reason"]

    with SessionLocal() as session:
        assert session.get(AgentRun, run_id).state == "failed"


async def test_a_run_can_be_asked_to_keep_no_events_at_all(book: None) -> None:
    run_id = await _run({}, events=NO_EVENTS)

    assert _events(run_id) == []
    with SessionLocal() as session:
        assert session.get(AgentRun, run_id).state == "completed"


async def test_a_person_dismissing_a_finding_joins_that_run_s_story(book: None) -> None:
    run_id = await _run(
        {
            FEES_WILL_EMPTY: [
                _calls(_write("The fee will empty these accounts", FEES_WILL_EMPTY)),
                _final_answer("One finding written."),
            ]
        },
        concurrency=1,
    )

    with SessionLocal() as session:
        insight = session.scalars(select(AgentInsight).where(AgentInsight.run_id == run_id)).one()
        transition_insight(
            session,
            insight,
            to_state="dismissed",
            reason="A person looked and this is already in hand.",
            decided_by="fa-1",
        )
        session.commit()

    events = _events(run_id)
    assert events[-1].kind == INSIGHT_DISMISSED
    assert events[-1].detail["to_state"] == "dismissed"
    assert events[-1].detail["decided_by_a_person"] is True
    assert events[-1].ordinal == max(row.ordinal for row in events)


def test_every_kind_the_agents_write_is_one_the_table_allows() -> None:
    written_by_the_agents = {
        RUN_STARTED,
        RUN_STATUS,
        RUN_COMPLETED,
        STEP_STARTED,
        STEP_COMPLETED,
        TOOL_STARTED,
        TOOL_COMPLETED,
        INSIGHT_CREATED,
        ERROR,
    }
    assert written_by_the_agents <= set(AGENT_EVENT_KINDS)


async def test_events_from_a_run_carry_no_client_ids(book: None) -> None:
    run_id = await _run(
        {
            FEES_WILL_EMPTY: [
                _calls(_write("The fee will empty these accounts", FEES_WILL_EMPTY)),
                _final_answer("One finding written."),
            ]
        },
        concurrency=1,
    )

    blob = str([row.detail for row in _events(run_id)])
    assert all(str(client_id) not in blob for client_id in CLIENT_IDS)
