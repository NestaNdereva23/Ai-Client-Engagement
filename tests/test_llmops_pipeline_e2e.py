"""End-to-end LLMOps pipeline: one generation run, traced, scored, reviewed,
and explainable months later.

This exercises the whole pipeline's acceptance check in one place, rather
than leaving it implicit across the individual unit tests each piece
already has: a generation writes a trace ref and a token row, an
evaluation persists and joins back to the run it scored, the reviewer's
outcome is recorded as ground truth carrying the run's own angle and
tier, and the reproducibility stamp that would explain the run months
later is both present and immutable.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db.models.audit import AuditLog
from app.db.models.llmops import (
    Evaluation,
    GenerationRun,
    LLMRequest,
    LLMResponse,
    TokenUsage,
    TraceRef,
)
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.session import SessionLocal
from app.llmops.ground_truth import ground_truth_rows
from app.llmops.telemetry import persist_generation_telemetry
from app.llmops.versions import persist_evaluation, persist_generation_run
from app.schemas.evaluation import EvaluationScores
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


def make_state(client_id: int) -> dict:
    return {
        "run_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "client_id": client_id,
        "product": "money market",
        "angle": "winback_habit",
        "priority_tier": "T2",
        "prompt_variant": "winback_habit",
        "rule_version": 4,
        "angle_catalog_version": 2,
        "data_date": date(2026, 7, 23),
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
                "input_tokens": 120,
                "output_tokens": 45,
                "latency_ms": 640,
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
    """A fund and a named client, ready to carry one run through the whole pipeline."""
    fund_id = 982
    client_id = 98201
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
        campaign = Campaign(name="pipeline e2e test campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id

    yield client_id, campaign_id

    with SessionLocal() as session:
        run_ids = session.scalars(
            select(GenerationRun.run_id).where(GenerationRun.client_id == client_id)
        ).all()
        message_ids = session.scalars(
            select(OutreachMessage.message_id).where(OutreachMessage.client_id == client_id)
        ).all()
        if message_ids:
            session.execute(delete(AuditLog).where(AuditLog.entity_id.in_(message_ids)))
            session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
            session.execute(
                delete(OutreachMessage).where(OutreachMessage.message_id.in_(message_ids))
            )
        if run_ids:
            request_ids = session.scalars(
                select(LLMRequest.request_id).where(LLMRequest.run_id.in_(run_ids))
            ).all()
            if request_ids:
                session.execute(delete(TokenUsage).where(TokenUsage.request_id.in_(request_ids)))
                session.execute(delete(LLMResponse).where(LLMResponse.request_id.in_(request_ids)))
                session.execute(delete(LLMRequest).where(LLMRequest.run_id.in_(run_ids)))
            session.execute(delete(TraceRef).where(TraceRef.run_id.in_(run_ids)))
            session.execute(delete(Evaluation).where(Evaluation.run_id.in_(run_ids)))
            session.execute(delete(GenerationRun).where(GenerationRun.run_id.in_(run_ids)))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_one_run_is_traced_scored_reviewed_and_explainable(scenario) -> None:
    client_id, campaign_id = scenario
    settings = make_settings()
    state = make_state(client_id)

    with SessionLocal() as session:
        run = persist_generation_run(session, state, settings)
        session.commit()
        persist_generation_telemetry(session, run, state)
        session.commit()
        run_id = run.run_id

    # A generation writes a trace ref and a token row.
    with SessionLocal() as session:
        assert session.get(TraceRef, run_id) is not None
        request = session.scalar(select(LLMRequest).where(LLMRequest.run_id == run_id))
        usage = session.scalar(
            select(TokenUsage).where(TokenUsage.request_id == request.request_id)
        )
        assert usage.input_tokens == 120
        assert usage.output_tokens == 45

    # Eval scores persist and join back to the run they scored.
    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        persist_evaluation(
            session,
            run,
            EvaluationScores(tone=4, compliance=5, grounding=5, personalization=4, notes="fine"),
            settings,
        )
        session.commit()

    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        message = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        message_id = message.message_id
        decide(session, message_id, outcome="approve", reviewer_id="fa-1")
        session.commit()

    # The review outcome is recorded as ground truth carrying the run's own
    # angle and tier, joined to the judge's own score on that same run.
    with SessionLocal() as session:
        rows = ground_truth_rows(session, message_angle="winback_habit", priority_tier="T2")
    matching = [r for r in rows if r.run_id == run_id]
    assert len(matching) == 1
    assert matching[0].outcome == "approve"
    assert matching[0].tone == 4

    # The reproducibility stamp is present.
    with SessionLocal() as session:
        stored = session.get(GenerationRun, run_id)
    assert stored.rule_version == 4
    assert stored.angle_catalog_version == 2
    assert stored.data_date == date(2026, 7, 23)


def test_the_reproducibility_stamp_is_immutable(scenario) -> None:
    """Nothing in this codebase updates a stored run: persist_generation_run
    is the only write path, and run_id is its primary key, so a second
    attempt under the same id fails outright rather than silently
    overwriting the original stamp.
    """
    client_id, _campaign_id = scenario
    settings = make_settings()
    state = make_state(client_id)

    with SessionLocal() as session:
        run = persist_generation_run(session, state, settings)
        session.commit()
        run_id = run.run_id

    tampered_state = dict(state)
    tampered_state["rule_version"] = 99
    tampered_state["angle_catalog_version"] = 99
    tampered_state["data_date"] = date(2099, 1, 1)

    with SessionLocal() as session, pytest.raises(IntegrityError):
        persist_generation_run(session, tampered_state, settings)

    with SessionLocal() as session:
        stored = session.get(GenerationRun, run_id)
    assert stored.rule_version == 4
    assert stored.angle_catalog_version == 2
    assert stored.data_date == date(2026, 7, 23)
