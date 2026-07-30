"""Registering prompt/model versions and stamping them on a generation run.

These prove both registries dedupe an unchanged config to the same row and
register a new one on a genuine change, persist_generation_run stamps a
terminal state with the right versions and stores ai_draft_content exactly as
generated, a rejected run with no validated content stores a null
ai_draft_content instead of erroring, and two runs sharing a config reuse the
same version rows rather than duplicating the registry.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.agents.email_agent import template_text
from app.config import Settings
from app.db.models.llmops import (
    Evaluation,
    GenerationRun,
    ModelVersion,
    PromptVersion,
    RubricVersion,
)
from app.db.models.models import Clients, Funds
from app.db.session import SessionLocal
from app.llmops.judge import rubric_text
from app.llmops.versions import (
    get_or_create_model_version,
    get_or_create_prompt_version,
    get_or_create_rubric_version,
    persist_evaluation,
    persist_generation_run,
)
from app.schemas.evaluation import EvaluationScores


def make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "llm_model": "claude-opus-5",
        "llm_temperature": None,
        "llm_max_tokens": 1024,
        "judge_llm_provider": "",
        "judge_llm_model": "",
        "judge_llm_temperature": None,
        "judge_llm_max_tokens": 512,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def client(db: None):
    """Seed one fund and one client so generation_runs' FK is satisfiable."""
    fund_id = 950
    client_id = 95001
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
            session.execute(delete(Evaluation).where(Evaluation.run_id.in_(run_ids)))
        session.execute(delete(GenerationRun).where(GenerationRun.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def accepted_state(client_id: int, *, prompt_variant: str = "habit_premium") -> dict:
    run_id = str(uuid4())
    return {
        "run_id": run_id,
        "trace_id": str(uuid4()),
        "client_id": client_id,
        "product": "money market",
        "angle": "winback_habit",
        "prompt_variant": prompt_variant,
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}, we miss you.",
        },
    }


def test_get_or_create_model_version_dedupes_an_unchanged_config(db: None) -> None:
    with SessionLocal() as session:
        first = get_or_create_model_version(
            session,
            provider="anthropic",
            model_id="claude-opus-5",
            temperature=None,
            max_tokens=1024,
        )
        second = get_or_create_model_version(
            session,
            provider="anthropic",
            model_id="claude-opus-5",
            temperature=None,
            max_tokens=1024,
        )
        session.commit()

    assert first.model_version_id == second.model_version_id


def test_get_or_create_model_version_registers_a_new_row_on_a_real_change(db: None) -> None:
    with SessionLocal() as session:
        base = get_or_create_model_version(
            session,
            provider="anthropic",
            model_id="claude-opus-5",
            temperature=None,
            max_tokens=1024,
        )
        different_tokens = get_or_create_model_version(
            session,
            provider="anthropic",
            model_id="claude-opus-5",
            temperature=None,
            max_tokens=2048,
        )
        different_temperature = get_or_create_model_version(
            session,
            provider="anthropic",
            model_id="claude-opus-5",
            temperature=0.7,
            max_tokens=1024,
        )
        different_model = get_or_create_model_version(
            session,
            provider="anthropic",
            model_id="claude-sonnet-5",
            temperature=None,
            max_tokens=1024,
        )
        session.commit()

    ids = {
        base.model_version_id,
        different_tokens.model_version_id,
        different_temperature.model_version_id,
        different_model.model_version_id,
    }
    assert len(ids) == 4


def test_get_or_create_prompt_version_dedupes_an_unchanged_variant(db: None) -> None:
    with SessionLocal() as session:
        first = get_or_create_prompt_version(
            session, channel="email", prompt_variant="habit_premium", angle="winback_habit"
        )
        second = get_or_create_prompt_version(
            session, channel="email", prompt_variant="habit_premium", angle="winback_habit"
        )
        session.commit()

    assert first.prompt_version_id == second.prompt_version_id
    assert first.template_text == template_text("habit_premium")


def test_get_or_create_prompt_version_registers_a_new_row_per_distinct_variant(db: None) -> None:
    with SessionLocal() as session:
        premium = get_or_create_prompt_version(
            session, channel="email", prompt_variant="habit_premium", angle="winback_habit"
        )
        standard = get_or_create_prompt_version(
            session, channel="email", prompt_variant="habit_standard", angle="winback_habit"
        )
        session.commit()

    assert premium.prompt_version_id != standard.prompt_version_id
    assert premium.template_text != standard.template_text


def test_persist_generation_run_stamps_and_stores_an_accepted_draft(client: int) -> None:
    state = accepted_state(client)
    settings = make_settings()

    with SessionLocal() as session:
        run = persist_generation_run(session, state, settings)
        session.commit()
        run_id = run.run_id

    with SessionLocal() as session:
        stored = session.get(GenerationRun, run_id)
        prompt_version = session.get(PromptVersion, stored.prompt_version_id)
        model_version = session.get(ModelVersion, stored.model_version_id)

    assert stored.status == "accepted"
    assert stored.client_id == client
    assert stored.ai_draft_content == state["raw_structured_output"]
    assert prompt_version.prompt_variant == "habit_premium"
    assert model_version.model_id == "claude-opus-5"
    assert model_version.max_tokens == 1024


def test_persist_generation_run_stores_a_null_draft_when_nothing_was_ever_validated(
    client: int,
) -> None:
    state = accepted_state(client)
    state["status"] = "rejected"
    state["failed_guardrail"] = "pii_scan"
    state["reason"] = "draft echoed a phone number"
    state["raw_structured_output"] = None

    with SessionLocal() as session:
        run = persist_generation_run(session, state, make_settings())
        session.commit()
        run_id = run.run_id

    with SessionLocal() as session:
        stored = session.get(GenerationRun, run_id)

    assert stored.status == "rejected"
    assert stored.failed_guardrail == "pii_scan"
    assert stored.ai_draft_content is None


def test_two_runs_with_the_same_config_share_one_version_row_each(client: int) -> None:
    settings = make_settings()

    with SessionLocal() as session:
        first = persist_generation_run(session, accepted_state(client), settings)
        second = persist_generation_run(session, accepted_state(client), settings)
        session.commit()

    assert first.prompt_version_id == second.prompt_version_id
    assert first.model_version_id == second.model_version_id
    # Distinct runs still get distinct run ids.
    assert first.run_id != second.run_id


def test_get_or_create_rubric_version_dedupes_the_unchanged_rubric(db: None) -> None:
    with SessionLocal() as session:
        first = get_or_create_rubric_version(session)
        second = get_or_create_rubric_version(session)
        session.commit()

    assert first.rubric_version_id == second.rubric_version_id
    assert first.rubric_text == rubric_text()


def make_scores(**overrides) -> EvaluationScores:
    defaults = {"tone": 4, "compliance": 5, "grounding": 5, "personalization": 3, "notes": "fine"}
    defaults.update(overrides)
    return EvaluationScores(**defaults)


def test_persist_evaluation_stamps_and_stores_a_score(client: int) -> None:
    settings = make_settings()
    with SessionLocal() as session:
        run = persist_generation_run(session, accepted_state(client), settings)
        session.commit()
        run_id = run.run_id

    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        evaluation = persist_evaluation(
            session, run, make_scores(tone=2, notes="a bit stiff"), settings
        )
        session.commit()
        evaluation_id = evaluation.evaluation_id

    with SessionLocal() as session:
        stored = session.get(Evaluation, evaluation_id)
        rubric_version = session.get(RubricVersion, stored.rubric_version_id)

    assert stored.run_id == run_id
    assert stored.tone == 2
    assert stored.notes == "a bit stiff"
    assert rubric_version.rubric_text == rubric_text()


def test_two_evaluations_for_different_runs_share_one_rubric_version(client: int) -> None:
    settings = make_settings()
    with SessionLocal() as session:
        first_run = persist_generation_run(session, accepted_state(client), settings)
        second_run = persist_generation_run(session, accepted_state(client), settings)
        session.commit()
        first_id, second_id = first_run.run_id, second_run.run_id

    with SessionLocal() as session:
        first_run = session.get(GenerationRun, first_id)
        second_run = session.get(GenerationRun, second_id)
        first_eval = persist_evaluation(session, first_run, make_scores(), settings)
        second_eval = persist_evaluation(session, second_run, make_scores(), settings)
        session.commit()

    assert first_eval.rubric_version_id == second_eval.rubric_version_id
    assert first_eval.evaluation_id != second_eval.evaluation_id


def test_persist_evaluation_stamps_the_judges_own_model_not_generations(client: int) -> None:
    """judge_llm_model differing from llm_model must be reflected in the stamped model_version."""
    generation_settings = make_settings(llm_provider="ollama", llm_model="phi4-mini")
    judge_settings = make_settings(
        llm_provider="ollama",
        llm_model="phi4-mini",
        judge_llm_provider="ollama",
        judge_llm_model="qwen3.5",
    )

    with SessionLocal() as session:
        run = persist_generation_run(session, accepted_state(client), generation_settings)
        session.commit()
        run_id = run.run_id

    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        evaluation = persist_evaluation(session, run, make_scores(), judge_settings)
        session.commit()
        evaluation_id = evaluation.evaluation_id

    with SessionLocal() as session:
        stored_run = session.get(GenerationRun, run_id)
        stored_eval = session.get(Evaluation, evaluation_id)
        generation_model = session.get(ModelVersion, stored_run.model_version_id)
        judge_model = session.get(ModelVersion, stored_eval.model_version_id)

    assert generation_model.model_id == "phi4-mini"
    assert judge_model.model_id == "qwen3.5"
    assert generation_model.model_version_id != judge_model.model_version_id
