"""Admin metrics: run aggregates and guardrail failure rates, sliced by angle,
tier, prompt version, and model version.

Covers cost/tokens/latency rolling up per run before averaging across a
slice, error rate reading rejected/total, a slice with no priced model
reporting a null cost rather than a wrong one, and guardrail failure rate
reading fail/total for one angle.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from app.config import Settings
from app.db.models.audit import AuditLog
from app.db.models.llmops import Evaluation, GenerationRun, LLMRequest, TokenUsage, TraceRef
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.session import SessionLocal
from app.llmops.telemetry import persist_generation_telemetry
from app.llmops.versions import persist_evaluation, persist_generation_run
from app.schemas.evaluation import EvaluationScores
from app.services.admin_metrics import (
    daily_generation_counts,
    funnel_counts,
    guardrail_failure_rates,
    judge_score_metrics,
    run_metrics,
)
from app.services.review import create_outreach_message, decide


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
            session.execute(delete(Evaluation).where(Evaluation.run_id.in_(run_ids)))
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


@pytest.fixture
def scenario_with_campaign(scenario):
    """scenario's client, plus a campaign to hang outreach messages off of."""
    client_id = scenario
    with SessionLocal() as session:
        campaign = Campaign(name="admin metrics funnel test campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id

    yield client_id, campaign_id

    with SessionLocal() as session:
        message_ids = session.scalars(
            select(OutreachMessage.message_id).where(OutreachMessage.campaign_id == campaign_id)
        ).all()
        if message_ids:
            session.execute(delete(AuditLog).where(AuditLog.entity_id.in_(message_ids)))
            session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
            session.execute(
                delete(OutreachMessage).where(OutreachMessage.message_id.in_(message_ids))
            )
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


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


def _persist_scored_run(session, client_id: int, *, tone: int, compliance: int, **overrides):
    run = _persist_run(session, client_id, **overrides)
    persist_evaluation(
        session,
        run,
        EvaluationScores(
            tone=tone, compliance=compliance, grounding=5, personalization=4, notes="fine"
        ),
        make_settings(),
    )
    session.commit()
    return run


def test_judge_score_metrics_averages_scores_within_one_angle_and_tier(scenario) -> None:
    client_id = scenario
    with SessionLocal() as session:
        _persist_scored_run(
            session, client_id, angle="see_what_changed", priority_tier="T1", tone=2, compliance=4
        )
        _persist_scored_run(
            session, client_id, angle="see_what_changed", priority_tier="T1", tone=4, compliance=4
        )

    with SessionLocal() as session:
        rows = judge_score_metrics(session, message_angle="see_what_changed", priority_tier="T1")

    assert len(rows) == 1
    row = rows[0]
    assert row.evaluation_count == 2
    assert row.avg_tone == pytest.approx(3.0)
    assert row.avg_compliance == pytest.approx(4.0)
    assert row.avg_grounding == pytest.approx(5.0)
    assert row.avg_personalization == pytest.approx(4.0)


def test_judge_score_metrics_excludes_a_run_the_judge_never_scored(scenario) -> None:
    client_id = scenario
    with SessionLocal() as session:
        _persist_scored_run(
            session, client_id, angle="rate_check", priority_tier="T4", tone=3, compliance=5
        )
        # Never scored: no persist_evaluation call.
        _persist_run(session, client_id, angle="rate_check", priority_tier="T4")

    with SessionLocal() as session:
        rows = judge_score_metrics(session, message_angle="rate_check", priority_tier="T4")

    assert len(rows) == 1
    assert rows[0].evaluation_count == 1
    assert rows[0].avg_tone == pytest.approx(3.0)


def _funnel_snapshot(session) -> dict:
    """funnel_counts is book-wide, so tests read a before/after delta rather
    than an absolute value, the same way test_api_clients.py's stale-contact
    count test does.
    """
    counts = funnel_counts(session)
    return {
        "generated": counts.generated,
        "accepted": counts.accepted,
        "guardrail_rejected": counts.guardrail_rejected,
        "pending_review": counts.pending_review,
        "approved": counts.approved,
        "review_rejected": counts.review_rejected,
        "escalated": counts.escalated,
        "held": counts.held,
    }


def test_funnel_counts_reflects_generated_accepted_and_guardrail_rejected_deltas(
    scenario,
) -> None:
    client_id = scenario
    with SessionLocal() as session:
        before = _funnel_snapshot(session)

    with SessionLocal() as session:
        _persist_run(
            session, client_id, angle="winback_habit", priority_tier="T2", status="accepted"
        )
        _persist_run(
            session,
            client_id,
            angle="winback_habit",
            priority_tier="T2",
            status="rejected",
            failed_guardrail="grounding",
        )

    with SessionLocal() as session:
        after = _funnel_snapshot(session)

    assert after["generated"] == before["generated"] + 2
    assert after["accepted"] == before["accepted"] + 1
    assert after["guardrail_rejected"] == before["guardrail_rejected"] + 1


def test_funnel_counts_reflects_a_review_outcome_delta(scenario_with_campaign) -> None:
    client_id, campaign_id = scenario_with_campaign
    with SessionLocal() as session:
        before = _funnel_snapshot(session)

    with SessionLocal() as session:
        run = _persist_run(session, client_id, angle="winback_habit", priority_tier="T2")
        message = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        decide(session, message.message_id, outcome="approve", reviewer_id="fa-1")
        session.commit()

    with SessionLocal() as session:
        after = _funnel_snapshot(session)

    assert after["approved"] == before["approved"] + 1
    # The message moved straight to approved; it never lingers as pending_review.
    assert after["pending_review"] == before["pending_review"]


def test_daily_generation_counts_reflects_todays_new_runs(scenario) -> None:
    client_id = scenario
    with SessionLocal() as session:
        before_rows = daily_generation_counts(session, days=1)
    before_generated = before_rows[0].generated if before_rows else 0
    before_accepted = before_rows[0].accepted if before_rows else 0

    with SessionLocal() as session:
        _persist_run(
            session, client_id, angle="winback_habit", priority_tier="T2", status="accepted"
        )
        _persist_run(
            session,
            client_id,
            angle="winback_habit",
            priority_tier="T2",
            status="rejected",
            failed_guardrail="grounding",
        )

    with SessionLocal() as session:
        after_rows = daily_generation_counts(session, days=1)

    assert len(after_rows) == 1
    assert after_rows[0].day == date.today()
    assert after_rows[0].generated == before_generated + 2
    assert after_rows[0].accepted == before_accepted + 1
