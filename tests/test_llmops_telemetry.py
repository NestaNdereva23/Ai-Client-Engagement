"""Persisting a run's llm_calls/tool_calls into their own tables.

These prove one llm_requests/llm_responses/token_usage row is written per
llm_calls entry (including a null raw_output for a pii_scan-blocked attempt),
one tool_calls row per tool_calls entry, and a trace_refs row carrying the
tracer's resolved URL when a tracer is given.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.config import Settings
from app.db.models.llmops import (
    GenerationRun,
    LLMRequest,
    LLMResponse,
    TokenUsage,
    ToolCall,
    TraceRef,
)
from app.db.models.models import Clients, Funds
from app.db.session import SessionLocal
from app.llmops.telemetry import persist_generation_telemetry
from app.llmops.versions import persist_generation_run


def make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "llm_model": "claude-opus-5",
        "llm_temperature": None,
        "llm_max_tokens": 1024,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def client(db: None):
    fund_id = 951
    client_id = 95101
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund"))
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

    yield client_id

    with SessionLocal() as session:
        run_ids = session.scalars(
            select(GenerationRun.run_id).where(GenerationRun.client_id == client_id)
        ).all()
        if run_ids:
            request_ids = session.scalars(
                select(LLMRequest.request_id).where(LLMRequest.run_id.in_(run_ids))
            ).all()
            if request_ids:
                session.execute(delete(TokenUsage).where(TokenUsage.request_id.in_(request_ids)))
                session.execute(delete(LLMResponse).where(LLMResponse.request_id.in_(request_ids)))
                session.execute(delete(LLMRequest).where(LLMRequest.request_id.in_(request_ids)))
            session.execute(delete(ToolCall).where(ToolCall.run_id.in_(run_ids)))
            session.execute(delete(TraceRef).where(TraceRef.run_id.in_(run_ids)))
        session.execute(delete(GenerationRun).where(GenerationRun.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def accepted_state(client_id: int, **overrides) -> dict:
    state = {
        "run_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "client_id": client_id,
        "product": "money market",
        "angle": "winback_habit",
        "prompt_variant": "habit_premium",
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}, we miss you.",
        },
        "llm_calls": [
            {
                "attempt": 1,
                "system_prompt": "draft an email",
                "raw_output": "Dear {{first_name}}, we miss you.",
                "input_tokens": 40,
                "output_tokens": 12,
                "latency_ms": 350,
            }
        ],
        "tool_calls": [
            {
                "tool_name": "context_fetch",
                "input": {"client_id": client_id, "product": "money market"},
                "output": {"angle": "winback_habit"},
            },
            {
                "tool_name": "rag_retrieval",
                "input": {"product": "money market"},
                "output": {"chunk_count": 2},
            },
        ],
    }
    state.update(overrides)
    return state


class FakeTracer:
    def __init__(self, url: str | None) -> None:
        self.url = url
        self.seen_trace_ids: list[str] = []

    def get_trace_url(self, trace_id: str) -> str | None:
        self.seen_trace_ids.append(trace_id)
        return self.url


def test_persists_one_request_response_and_token_usage_row_per_llm_call(client: int) -> None:
    state = accepted_state(client)
    settings = make_settings()

    with SessionLocal() as session:
        run = persist_generation_run(session, state, settings)
        persist_generation_telemetry(session, run, state)
        session.commit()
        run_id = run.run_id

    with SessionLocal() as session:
        requests = session.scalars(select(LLMRequest).where(LLMRequest.run_id == run_id)).all()
        assert len(requests) == 1
        request = requests[0]
        assert request.attempt == 1
        assert request.system_prompt == "draft an email"
        assert request.model_version_id == run.model_version_id

        response = session.scalar(
            select(LLMResponse).where(LLMResponse.request_id == request.request_id)
        )
        assert response.raw_output == "Dear {{first_name}}, we miss you."
        assert response.latency_ms == 350

        usage = session.scalar(
            select(TokenUsage).where(TokenUsage.request_id == request.request_id)
        )
        assert usage.input_tokens == 40
        assert usage.output_tokens == 12


def test_a_pii_scan_blocked_attempt_stores_a_null_raw_output(client: int) -> None:
    state = accepted_state(
        client,
        llm_calls=[
            {
                "attempt": 1,
                "system_prompt": "draft an email",
                "raw_output": None,
                "input_tokens": 30,
                "output_tokens": 9,
                "latency_ms": 200,
            }
        ],
    )
    settings = make_settings()

    with SessionLocal() as session:
        run = persist_generation_run(session, state, settings)
        persist_generation_telemetry(session, run, state)
        session.commit()
        run_id = run.run_id

    with SessionLocal() as session:
        request = session.scalar(select(LLMRequest).where(LLMRequest.run_id == run_id))
        response = session.scalar(
            select(LLMResponse).where(LLMResponse.request_id == request.request_id)
        )
        assert response.raw_output is None


def test_persists_one_row_per_retry_attempt(client: int) -> None:
    state = accepted_state(
        client,
        llm_calls=[
            {
                "attempt": 1,
                "system_prompt": "draft an email",
                "raw_output": None,
                "input_tokens": 30,
                "output_tokens": 9,
                "latency_ms": 200,
            },
            {
                "attempt": 2,
                "system_prompt": "draft an email",
                "raw_output": "Dear {{first_name}}, welcome back.",
                "input_tokens": 31,
                "output_tokens": 15,
                "latency_ms": 220,
            },
        ],
    )
    settings = make_settings()

    with SessionLocal() as session:
        run = persist_generation_run(session, state, settings)
        persist_generation_telemetry(session, run, state)
        session.commit()
        run_id = run.run_id

    with SessionLocal() as session:
        requests = session.scalars(
            select(LLMRequest).where(LLMRequest.run_id == run_id).order_by(LLMRequest.attempt)
        ).all()
        assert [r.attempt for r in requests] == [1, 2]


def test_persists_one_row_per_tool_call(client: int) -> None:
    state = accepted_state(client)
    settings = make_settings()

    with SessionLocal() as session:
        run = persist_generation_run(session, state, settings)
        persist_generation_telemetry(session, run, state)
        session.commit()
        run_id = run.run_id

    with SessionLocal() as session:
        tool_calls = session.scalars(select(ToolCall).where(ToolCall.run_id == run_id)).all()
        names = {t.tool_name for t in tool_calls}
        assert names == {"context_fetch", "rag_retrieval"}
        rag = next(t for t in tool_calls if t.tool_name == "rag_retrieval")
        assert rag.tool_output == {"chunk_count": 2}


def test_trace_ref_carries_the_tracers_resolved_url(client: int) -> None:
    state = accepted_state(client)
    settings = make_settings()
    tracer = FakeTracer(url="http://localhost:3000/trace/abc")

    with SessionLocal() as session:
        run = persist_generation_run(session, state, settings)
        persist_generation_telemetry(session, run, state, tracer=tracer)
        session.commit()
        run_id, trace_id = run.run_id, run.trace_id

    assert tracer.seen_trace_ids == [trace_id]

    with SessionLocal() as session:
        ref = session.get(TraceRef, run_id)
        assert ref.trace_id == trace_id
        assert ref.trace_url == "http://localhost:3000/trace/abc"


def test_trace_ref_url_is_null_without_a_tracer(client: int) -> None:
    state = accepted_state(client)
    settings = make_settings()

    with SessionLocal() as session:
        run = persist_generation_run(session, state, settings)
        persist_generation_telemetry(session, run, state)
        session.commit()
        run_id = run.run_id

    with SessionLocal() as session:
        ref = session.get(TraceRef, run_id)
        assert ref.trace_url is None
