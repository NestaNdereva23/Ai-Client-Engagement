"""The intelligence agent, against a fake model.

These cover what a run has to get right: several findings written and read
back whole, a run that looks everywhere and honestly finds nothing, a group
that spends its turns without settling, a write tool refusing a bad call,
and one group failing while the rest of the run finishes. One more proves
the groups investigated side by side come out the same as one after
another.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date

import pytest
from sqlalchemy import delete, select

from app.agents import intelligence as intelligence_module
from app.agents.group_questions import EVERYTHING_ELSE, GROUP_QUESTIONS, question_for
from app.agents.intelligence import (
    FAILED,
    FOUND_NOTHING,
    RAN_OUT_OF_TURNS,
    WROTE_SOMETHING,
    run_intelligence_agent,
)
from app.agents.watchlist import (
    FEES_WILL_EMPTY,
    GROUP_NAMES,
    VERY_SMALL_AND_QUIET,
    WatchlistThresholds,
)
from app.db.async_session import dispose_async_engine
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_insight import AgentInsight, AgentInsightFact
from app.db.models.agent_run import AgentRun, AgentToolCall
from app.db.models.audit import AuditLog
from app.db.models.risk import ClientRiskFeatures
from app.db.session import SessionLocal
from app.privacy.llm_client import ConversationTurn, LLMClientError, LLMUsage, ToolUseRequest

FUND_ID = 9832
CLIENT_IDS = tuple(range(983201, 983207))

AS_OF = date(2026, 9, 8)

THRESHOLDS = WatchlistThresholds(
    new_client_days=30,
    months_until_empty=6.0,
    small_balance=1_000.0,
    awaiting_call_days=2,
)

_USAGE = LLMUsage(input_tokens=10, output_tokens=10)

_GROUP_MARKER = "Tonight you are looking at one group: "

HIGH_RISK_FILTER = [{"field": "risk_band", "op": "eq", "value": "High"}]


class ScriptedAsyncClient:
    """Answers each group's investigation from its own list of turns.

    A group with no script of its own replies once with plain text, which
    is how a run says it looked and had nothing to add.
    """

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
        entry = queue.pop(0)
        return entry(messages) if callable(entry) else entry


class FakeTracer:
    """Records every span, its place in the tree, and what it was closed with."""

    def __init__(self) -> None:
        self.spans: list[dict] = []
        self.flushes = 0

    def start_span(
        self, *, trace_id, name, input, metadata=None, as_type="span", model=None, parent=None
    ):
        span = {
            "name": name,
            "input": input,
            "output": None,
            "as_type": as_type,
            "model": model,
            "metadata": metadata,
            "parent": None if parent is None else parent["name"],
            "usage_details": None,
            "level": None,
        }
        self.spans.append(span)
        return span

    def end_span(self, handle, *, output, usage_details=None, level=None, status_message=None):
        handle["output"] = output
        handle["usage_details"] = usage_details
        handle["level"] = level

    def named(self, name: str) -> list[dict]:
        return [span for span in self.spans if span["name"] == name]

    def of_type(self, as_type: str) -> list[dict]:
        return [span for span in self.spans if span["as_type"] == as_type]

    def get_trace_url(self, trace_id):
        return f"https://langfuse.example/traces/{trace_id}"

    def flush(self) -> None:
        self.flushes += 1

    def shutdown(self) -> None:
        return None


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


def _raising_turn(*_args, **_kwargs):
    raise LLMClientError("the model server is unreachable")


def _last_insight_id(messages) -> int:
    found = re.findall(r"insight_id[^0-9]{0,8}(\d+)", json.dumps(messages, default=str))
    assert found, "no insight id came back from write_insight"
    return int(found[-1])


def _add_fact_for_the_insight_just_written(messages) -> ConversationTurn:
    return _calls(
        (
            "add_fact",
            {
                "insight_id": _last_insight_id(messages),
                "fact_text": "clients in this group whose risk band is high",
                "fact_value": "4",
                "source_table": "client_risk_features",
                "conditions": HIGH_RISK_FILTER,
            },
        )
    )


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


def _outcome_names(session, run_id: int) -> dict[str, str]:
    rows = session.scalars(
        select(AuditLog.detail).where(
            AuditLog.run_id == str(run_id), AuditLog.action == "investigate_group"
        )
    ).all()
    return {row["group_name"]: row["outcome"] for row in rows}


def test_every_group_has_a_question_and_the_last_one_has_no_filter() -> None:
    assert set(GROUP_QUESTIONS) == set(GROUP_NAMES) | {EVERYTHING_ELSE}
    assert all(question.endswith("?") for question in GROUP_QUESTIONS.values())
    assert "none of the other groups would catch" in question_for(EVERYTHING_ELSE)


async def test_a_run_writes_several_findings_and_hangs_a_fact_on_one(book: None) -> None:
    scripts = {
        FEES_WILL_EMPTY: [
            _calls(
                _write("The fee will empty these accounts", FEES_WILL_EMPTY),
                _write("A few of them still pay in", FEES_WILL_EMPTY, kind="opportunity"),
            ),
            _final_answer("Two findings written."),
        ],
        VERY_SMALL_AND_QUIET: [
            _calls(_write("These are small enough to leave alone", VERY_SMALL_AND_QUIET)),
            _add_fact_for_the_insight_just_written,
            _final_answer("One finding written, with its number."),
        ],
    }

    run_id = await _run(scripts)

    with SessionLocal() as session:
        run = session.get(AgentRun, run_id)
        insights = session.scalars(select(AgentInsight).where(AgentInsight.run_id == run_id)).all()
        facts = session.scalars(
            select(AgentInsightFact).where(
                AgentInsightFact.insight_id.in_([row.insight_id for row in insights])
            )
        ).all()
        outcomes = _outcome_names(session, run_id)

    assert run.state == "completed"
    assert len(insights) == 3
    assert outcomes[FEES_WILL_EMPTY] == WROTE_SOMETHING
    assert outcomes[VERY_SMALL_AND_QUIET] == WROTE_SOMETHING
    assert len(facts) == 1
    assert facts[0].source_filter == {"conditions": HIGH_RISK_FILTER}
    assert "3 findings written" in run.summary


async def test_a_group_may_produce_several_findings_and_another_none(book: None) -> None:
    scripts = {
        FEES_WILL_EMPTY: [
            _calls(
                _write("One", FEES_WILL_EMPTY),
                _write("Two", FEES_WILL_EMPTY),
                _write("Three", FEES_WILL_EMPTY),
            ),
            _final_answer("Three findings written."),
        ],
        VERY_SMALL_AND_QUIET: [
            _calls(_dismiss(VERY_SMALL_AND_QUIET, "every balance here is too small to chase")),
            _final_answer("Nothing worth raising."),
        ],
    }

    run_id = await _run(scripts)

    with SessionLocal() as session:
        insights = session.scalars(select(AgentInsight).where(AgentInsight.run_id == run_id)).all()
        outcomes = _outcome_names(session, run_id)

    assert len(insights) == 3
    assert outcomes[FEES_WILL_EMPTY] == WROTE_SOMETHING
    assert outcomes[VERY_SMALL_AND_QUIET] == FOUND_NOTHING


async def test_a_run_that_finds_nothing_says_so(book: None) -> None:
    scripts = {
        name: [
            _calls(_dismiss(name, "nothing here needs a person tonight")),
            _final_answer("Looked, found nothing."),
        ]
        for name in (*GROUP_NAMES, EVERYTHING_ELSE)
    }

    run_id = await _run(scripts)

    with SessionLocal() as session:
        run = session.get(AgentRun, run_id)
        insights = session.scalars(select(AgentInsight).where(AgentInsight.run_id == run_id)).all()
        outcomes = _outcome_names(session, run_id)

    assert run.state == "completed"
    assert insights == []
    assert "Nothing worth raising was found this run." in run.summary
    assert set(outcomes.values()) == {FOUND_NOTHING}


async def test_a_group_that_runs_out_of_turns_does_not_stop_the_rest(book: None) -> None:
    scripts = {
        FEES_WILL_EMPTY: [
            _calls(("list_groups", {"as_of": AS_OF.isoformat()})),
            _calls(("list_groups", {"as_of": AS_OF.isoformat()})),
        ],
        VERY_SMALL_AND_QUIET: [
            _calls(_write("Found while another group stalled", VERY_SMALL_AND_QUIET)),
            _final_answer("One finding written."),
        ],
    }

    run_id = await _run(scripts, max_turns=2)

    with SessionLocal() as session:
        run = session.get(AgentRun, run_id)
        insights = session.scalars(select(AgentInsight).where(AgentInsight.run_id == run_id)).all()
        outcomes = _outcome_names(session, run_id)

    assert run.state == "completed"
    assert outcomes[FEES_WILL_EMPTY] == RAN_OUT_OF_TURNS
    assert outcomes[VERY_SMALL_AND_QUIET] == WROTE_SOMETHING
    assert len(insights) == 1
    assert "1 ran out of turns before finishing." in run.summary


async def test_a_write_tool_refusing_is_recorded_and_the_group_carries_on(book: None) -> None:
    scripts = {
        FEES_WILL_EMPTY: [
            _calls(_write("A finding of an unknown kind", FEES_WILL_EMPTY, kind="hunch")),
            _calls(_write("The same finding, written properly", FEES_WILL_EMPTY)),
            _final_answer("Corrected and written."),
        ],
    }

    run_id = await _run(scripts)

    with SessionLocal() as session:
        run = session.get(AgentRun, run_id)
        insights = session.scalars(select(AgentInsight).where(AgentInsight.run_id == run_id)).all()
        refusals = session.scalars(
            select(AgentToolCall.tool_output).where(
                AgentToolCall.run_id == run_id, AgentToolCall.tool_name == "write_insight"
            )
        ).all()

    assert run.state == "completed"
    assert len(insights) == 1
    assert insights[0].title == "The same finding, written properly"
    assert any(output.get("error") == "unknown_kind" for output in refusals)


async def test_one_group_failing_leaves_the_others_finished(book: None) -> None:
    scripts = {
        FEES_WILL_EMPTY: [_raising_turn],
        VERY_SMALL_AND_QUIET: [
            _calls(_write("Found while another group failed", VERY_SMALL_AND_QUIET)),
            _final_answer("One finding written."),
        ],
    }

    run_id = await _run(scripts)

    with SessionLocal() as session:
        run = session.get(AgentRun, run_id)
        insights = session.scalars(select(AgentInsight).where(AgentInsight.run_id == run_id)).all()
        outcomes = _outcome_names(session, run_id)
        actions = set(
            session.scalars(select(AuditLog.action).where(AuditLog.run_id == str(run_id))).all()
        )

    assert run.state == "completed"
    assert run.failure_reason is None
    assert outcomes[FEES_WILL_EMPTY] == FAILED
    assert outcomes[VERY_SMALL_AND_QUIET] == WROTE_SOMETHING
    assert len(insights) == 1
    assert {"gather", "investigate_group", "report"} <= actions
    assert f"could not be looked at at all: {FEES_WILL_EMPTY}" in run.summary


async def test_a_failing_group_never_leaves_a_half_written_finding(book: None) -> None:
    scripts = {
        FEES_WILL_EMPTY: [
            _calls(_write("Written just before the model died", FEES_WILL_EMPTY)),
            _raising_turn,
        ],
    }

    run_id = await _run(scripts)

    with SessionLocal() as session:
        insights = session.scalars(select(AgentInsight).where(AgentInsight.run_id == run_id)).all()
        outcomes = _outcome_names(session, run_id)

    assert insights == []
    assert outcomes[FEES_WILL_EMPTY] == FAILED


def _side_by_side_scripts() -> dict[str, list]:
    return {
        FEES_WILL_EMPTY: [
            _calls(
                _write("The fee will empty these accounts", FEES_WILL_EMPTY),
                _write("Some still pay in", FEES_WILL_EMPTY, kind="opportunity"),
            ),
            _final_answer("Two findings written."),
        ],
        VERY_SMALL_AND_QUIET: [
            _calls(_dismiss(VERY_SMALL_AND_QUIET, "too small to be worth a message")),
            _final_answer("Nothing worth raising."),
        ],
        EVERYTHING_ELSE: [
            _calls(_write("A story none of the groups would catch", EVERYTHING_ELSE)),
            _final_answer("One finding written."),
        ],
    }


def _run_shape(run_id: int) -> tuple:
    with SessionLocal() as session:
        run = session.get(AgentRun, run_id)
        titles = sorted(
            session.scalars(select(AgentInsight.title).where(AgentInsight.run_id == run_id)).all()
        )
        return run.summary, titles, _outcome_names(session, run_id)


async def test_groups_side_by_side_give_the_same_result_as_one_after_another(book: None) -> None:
    one_at_a_time = await _run(_side_by_side_scripts(), concurrency=1)
    sequential = _run_shape(one_at_a_time)

    with SessionLocal() as session:
        _purge(session)

    side_by_side = await _run(_side_by_side_scripts(), concurrency=4)
    concurrent = _run_shape(side_by_side)

    assert concurrent == sequential


async def _traced_run(scripts: dict[str, list], **kwargs) -> tuple[int, FakeTracer]:
    tracer = FakeTracer()
    with SessionLocal() as session:
        run = await run_intelligence_agent(
            session,
            trigger="manual",
            llm_client=ScriptedAsyncClient(scripts),
            as_of=AS_OF,
            tracer=tracer,
            **kwargs,
        )
        return run.run_id, tracer


async def test_every_step_traces_without_a_client_id(book: None) -> None:
    scripts = {
        FEES_WILL_EMPTY: [
            _calls(_write("The fee will empty these accounts", FEES_WILL_EMPTY)),
            _final_answer("One finding written."),
        ],
    }

    _run_id, tracer = await _traced_run(scripts)

    steps = [span["name"] for span in tracer.spans if span["parent"] is None]
    assert steps == ["gather", "investigate", "record", "summarise"]
    rendered = json.dumps(tracer.spans, default=str)
    for client_id in CLIENT_IDS:
        assert str(client_id) not in rendered
    assert str(FUND_ID) not in rendered


async def test_each_group_is_a_span_of_its_own_under_the_investigate_step(book: None) -> None:
    scripts = {
        FEES_WILL_EMPTY: [
            _calls(_write("The fee will empty these accounts", FEES_WILL_EMPTY)),
            _final_answer("One finding written."),
        ],
    }

    _run_id, tracer = await _traced_run(scripts)

    groups = tracer.of_type("agent")
    assert {span["name"] for span in groups} == set(GROUP_QUESTIONS)
    assert {span["parent"] for span in groups} == {"investigate"}
    fees = tracer.named(FEES_WILL_EMPTY)[0]
    assert fees["input"]["question"] == question_for(FEES_WILL_EMPTY)
    assert fees["output"]["outcome"] == WROTE_SOMETHING
    assert fees["output"]["insight_titles"] == ["The fee will empty these accounts"]


async def test_every_model_call_is_a_generation_span_with_its_model_and_tokens(
    book: None,
) -> None:
    scripts = {
        FEES_WILL_EMPTY: [
            _calls(_write("The fee will empty these accounts", FEES_WILL_EMPTY)),
            _final_answer("One finding written."),
        ],
    }

    _run_id, tracer = await _traced_run(scripts)

    calls = [span for span in tracer.of_type("generation") if span["parent"] == FEES_WILL_EMPTY]
    assert len(calls) == 2
    for span in calls:
        assert span["model"] == ScriptedAsyncClient.model
        assert span["usage_details"] == {
            "input": _USAGE.input_tokens,
            "output": _USAGE.output_tokens,
        }
        assert span["input"]["system"].startswith("You look for things worth acting on")
        assert isinstance(span["input"]["messages"], list)
    assert calls[0]["output"]["tool_calls"][0]["tool_name"] == "write_insight"
    assert calls[1]["output"]["text"] == "One finding written."


async def test_every_tool_call_is_a_span_carrying_what_was_asked_and_answered(book: None) -> None:
    measure = ("measure_slice", {"conditions": HIGH_RISK_FILTER, "measures": ["client_count"]})
    scripts = {
        FEES_WILL_EMPTY: [
            _calls(measure, _write("A finding of an unknown kind", FEES_WILL_EMPTY, kind="hunch")),
            _final_answer("Looked and got one refusal."),
        ],
    }

    _run_id, tracer = await _traced_run(scripts)

    asked = tracer.named("measure_slice")[0]
    assert asked["as_type"] == "tool"
    assert asked["parent"] == FEES_WILL_EMPTY
    assert asked["input"] == {"conditions": HIGH_RISK_FILTER, "measures": ["client_count"]}
    assert "measures" in asked["output"]
    assert asked["level"] is None

    refused = tracer.named("write_insight")[0]
    assert refused["output"]["error"] == "unknown_kind"
    assert refused["level"] == "WARNING"


async def test_a_failing_group_is_marked_on_the_trace_and_the_run_still_flushes(
    book: None,
) -> None:
    scripts = {FEES_WILL_EMPTY: [_raising_turn]}

    _run_id, tracer = await _traced_run(scripts)

    failed = tracer.named(FEES_WILL_EMPTY)[0]
    assert failed["level"] == "ERROR"
    assert failed["output"]["outcome"] == FAILED
    assert tracer.flushes == 1


async def test_the_run_records_its_tokens_and_where_to_read_the_trace(book: None) -> None:
    scripts = {
        FEES_WILL_EMPTY: [
            _calls(_write("The fee will empty these accounts", FEES_WILL_EMPTY)),
            _final_answer("One finding written."),
        ],
    }

    run_id, _tracer = await _traced_run(scripts)

    with SessionLocal() as session:
        detail = session.scalars(
            select(AuditLog.detail).where(
                AuditLog.run_id == str(run_id), AuditLog.action == "report"
            )
        ).one()

    groups = len(GROUP_QUESTIONS)
    assert detail["model_calls"] == groups + 1
    assert detail["input_tokens"] == (groups + 1) * _USAGE.input_tokens
    assert detail["output_tokens"] == (groups + 1) * _USAGE.output_tokens
    assert detail["trace_url"].startswith("https://langfuse.example/traces/")


async def test_groups_asking_their_own_questions_side_by_side_each_record_a_tool_call(
    book: None,
) -> None:
    measure = ("measure_slice", {"conditions": HIGH_RISK_FILTER, "measures": ["client_count"]})
    scripts = {
        name: [_calls(measure), _final_answer("Counted.")]
        for name in (*GROUP_NAMES, EVERYTHING_ELSE)
    }

    run_id = await _run(scripts, concurrency=len(GROUP_QUESTIONS))

    with SessionLocal() as session:
        run = session.get(AgentRun, run_id)
        calls = session.scalars(
            select(AgentToolCall).where(
                AgentToolCall.run_id == run_id, AgentToolCall.tool_name == "measure_slice"
            )
        ).all()

    assert run.state == "completed"
    assert len(calls) == len(GROUP_QUESTIONS)
    assert len({call.ordinal for call in calls}) == len(calls)
    assert all("measures" in call.tool_output for call in calls)


async def test_a_run_over_every_group_is_far_quicker_than_one_group_at_a_time(book: None) -> None:
    wait = 0.2

    class SlowClient(ScriptedAsyncClient):
        async def aconverse(self, *, system, messages, tools=()):
            await asyncio.sleep(wait)
            return await super().aconverse(system=system, messages=messages, tools=tools)

    scripts = {name: [_final_answer("Nothing to add.")] for name in GROUP_QUESTIONS}
    groups = len(GROUP_QUESTIONS)

    with SessionLocal() as session:
        started = time.perf_counter()
        await run_intelligence_agent(
            session,
            trigger="manual",
            llm_client=SlowClient(scripts),
            as_of=AS_OF,
            concurrency=groups,
        )
        took = time.perf_counter() - started

    assert took < wait * groups


async def test_one_group_spending_its_queries_does_not_starve_another(book: None) -> None:
    measure = ("measure_slice", {"conditions": HIGH_RISK_FILTER, "measures": ["client_count"]})
    scripts = {
        name: [_calls(measure), _calls(measure), _final_answer("Counted what I could.")]
        for name in (FEES_WILL_EMPTY, VERY_SMALL_AND_QUIET)
    }

    run_id = await _run(scripts, query_budget=1)

    with SessionLocal() as session:
        outputs = session.scalars(
            select(AgentToolCall.tool_output).where(
                AgentToolCall.run_id == run_id, AgentToolCall.tool_name == "measure_slice"
            )
        ).all()

    answered = [output for output in outputs if "measures" in output]
    refused = [output for output in outputs if output.get("error") == "budget_spent"]
    assert len(answered) == 2
    assert len(refused) == 2
