"""Admin metrics: run aggregates and guardrail failure rates, sliced by angle,
tier, prompt version, and model version.

Covers cost/tokens/latency rolling up per run before averaging across a
slice, error rate reading rejected/total, a slice with no priced model
reporting a null cost rather than a wrong one, and guardrail failure rate
reading fail/total for one angle.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from app.config import Settings
from app.db.models.llmops import GenerationRun, LLMRequest, TokenUsage, TraceRef
from app.db.models.models import Clients, Funds, PiiVault
from app.db.session import SessionLocal
from app.llmops.telemetry import persist_generation_telemetry
from app.llmops.versions import persist_generation_run
from app.services.admin_metrics import guardrail_failure_rates, run_metrics


def make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "llm_model": "claude-haiku-4-5-20251001",
        "llm_temperature": None,
        "llm_max_tokens": 1024,
        "judge_llm_provider": "",
        "judge_llm_model": "",
        "judge_llm_temperature": None,
        "judge_llm_max_tokens": 512,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def make_state(
    client_id: int,
    *,
    angle: str,
    priority_tier: str,
    status: str = "accepted",
    failed_guardrail: str | None = None,
    input_tokens: int | None = 100,
    output_tokens: int | None = 50,
    latency_ms: int = 800,
) -> dict:
    return {
        "run_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "client_id": client_id,
        "product": "money market",
        "angle": angle,
        "priority_tier": priority_tier,
        "prompt_variant": angle,
        "status": status,
        "attempts": 1,
        "failed_guardrail": failed_guardrail,
        "reason": None,
        "raw_structured_output": (
            {"subject": "Come back to {{fund_name}}", "body": "Dear {{first_name}}, we miss you."}
            if status == "accepted"
            else None
        ),
        "llm_calls": [
            {
                "attempt": 1,
                "system_prompt": "system prompt text",
                "raw_output": "raw model output",
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        ],
        "tool_calls": [],
    }


@pytest.fixture
def roles(db: None):
    with SessionLocal() as session:
        exists = session.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'ace_restricted'"))
    if not exists:
        pytest.skip("boundary roles not present; run alembic upgrade head")


@pytest.fixture
def scenario(roles):
    """A client, ready to grow generation runs and their telemetry on demand."""
    fund_id = 981
    client_id = 98101
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
        session.add(PiiVault(client_id=client_id, client_name="Jane Doe"))
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
                from app.db.models.llmops import LLMResponse, ToolCall

                session.execute(delete(LLMResponse).where(LLMResponse.request_id.in_(request_ids)))
                session.execute(delete(LLMRequest).where(LLMRequest.run_id.in_(run_ids)))
                session.execute(delete(ToolCall).where(ToolCall.run_id.in_(run_ids)))
            session.execute(delete(TraceRef).where(TraceRef.run_id.in_(run_ids)))
            session.execute(delete(GenerationRun).where(GenerationRun.run_id.in_(run_ids)))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def _persist_run(session, client_id: int, **state_overrides) -> GenerationRun:
    state = make_state(client_id, **state_overrides)
    run = persist_generation_run(session, state, make_settings())
    session.commit()
    persist_generation_telemetry(session, run, state)
    session.commit()
    return run


def test_run_metrics_rolls_up_tokens_and_cost_per_run_before_averaging(scenario) -> None:
    client_id = scenario
    with SessionLocal() as session:
        _persist_run(
            session,
            client_id,
            angle="winback_habit",
            priority_tier="T2",
            input_tokens=100,
            output_tokens=50,
        )
        _persist_run(
            session,
            client_id,
            angle="winback_habit",
            priority_tier="T2",
            input_tokens=300,
            output_tokens=150,
        )

    with SessionLocal() as session:
        rows = run_metrics(session, message_angle="winback_habit", priority_tier="T2")

    assert len(rows) == 1
    row = rows[0]
    assert row.run_count == 2
    assert row.error_rate == 0.0
    assert row.avg_input_tokens == 200.0
    assert row.avg_output_tokens == 100.0
    assert row.avg_latency_ms == 800.0
    # claude-haiku-4-5-20251001 is priced: $1/$5 per 1M tokens.
    expected_cost_run_1 = 100 * (1.00 / 1_000_000) + 50 * (5.00 / 1_000_000)
    expected_cost_run_2 = 300 * (1.00 / 1_000_000) + 150 * (5.00 / 1_000_000)
    assert row.avg_cost_usd == pytest.approx((expected_cost_run_1 + expected_cost_run_2) / 2)


def test_run_metrics_error_rate_reads_rejected_over_total(scenario) -> None:
    client_id = scenario
    with SessionLocal() as session:
        _persist_run(
            session, client_id, angle="pick_up_again", priority_tier="T3", status="accepted"
        )
        _persist_run(
            session,
            client_id,
            angle="pick_up_again",
            priority_tier="T3",
            status="rejected",
            failed_guardrail="grounding",
        )

    with SessionLocal() as session:
        rows = run_metrics(session, message_angle="pick_up_again", priority_tier="T3")

    assert len(rows) == 1
    assert rows[0].run_count == 2
    assert rows[0].error_rate == 0.5


def test_run_metrics_reports_null_cost_for_an_unpriced_model(scenario) -> None:
    client_id = scenario
    with SessionLocal() as session:
        state = make_state(client_id, angle="see_what_changed", priority_tier="T1")
        run = persist_generation_run(
            session, state, make_settings(llm_model="claude-unpriced-test-model")
        )
        session.commit()
        persist_generation_telemetry(session, run, state)
        session.commit()

    with SessionLocal() as session:
        rows = run_metrics(session, message_angle="see_what_changed", priority_tier="T1")

    assert len(rows) == 1
    assert rows[0].avg_cost_usd is None


def test_guardrail_failure_rates_reads_fail_over_total_for_one_angle(scenario) -> None:
    client_id = scenario
    with SessionLocal() as session:
        _persist_run(session, client_id, angle="rate_check", priority_tier="T4", status="accepted")
        _persist_run(
            session,
            client_id,
            angle="rate_check",
            priority_tier="T4",
            status="rejected",
            failed_guardrail="structured_output",
        )
        _persist_run(
            session,
            client_id,
            angle="rate_check",
            priority_tier="T4",
            status="rejected",
            failed_guardrail="structured_output",
        )

    with SessionLocal() as session:
        rows = guardrail_failure_rates(session, message_angle="rate_check")

    assert len(rows) == 1
    row = rows[0]
    assert row.failed_guardrail == "structured_output"
    assert row.fail_count == 2
    assert row.run_count == 3
    assert row.failure_rate == pytest.approx(2 / 3)
