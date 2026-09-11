from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.email_agent import template_text
from app.config import Settings
from app.db.models.llmops import (
    Evaluation,
    GenerationRun,
    ModelVersion,
    PromptVersion,
    RubricVersion,
)
from app.llmops.judge import rubric_text
from app.privacy.llm_client import resolve_judge_model_config
from app.schemas.evaluation import EvaluationScores

EMAIL_CHANNEL = "email"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def get_or_create_model_version(
    session: Session,
    *,
    provider: str,
    model_id: str,
    temperature: float | None,
    max_tokens: int,
) -> ModelVersion:
    config_hash = _hash(f"{provider}|{model_id}|{temperature}|{max_tokens}")
    existing = session.scalar(select(ModelVersion).where(ModelVersion.config_hash == config_hash))
    if existing is not None:
        return existing

    row = ModelVersion(
        provider=provider,
        model_id=model_id,
        temperature=temperature,
        max_tokens=max_tokens,
        config_hash=config_hash,
    )
    session.add(row)
    session.flush()
    return row


def get_or_create_prompt_version(
    session: Session,
    *,
    channel: str,
    prompt_variant: str,
    angle: str,
) -> PromptVersion:
    text = template_text(prompt_variant or None)
    template_hash = _hash(f"{channel}|{prompt_variant}|{angle}|{text}")
    existing = session.scalar(
        select(PromptVersion).where(PromptVersion.template_hash == template_hash)
    )
    if existing is not None:
        return existing

    row = PromptVersion(
        channel=channel,
        prompt_variant=prompt_variant,
        angle=angle,
        template_text=text,
        template_hash=template_hash,
    )
    session.add(row)
    session.flush()
    return row


def persist_generation_run(
    session: Session,
    state: Mapping[str, Any],
    settings: Settings,
    *,
    channel: str = EMAIL_CHANNEL,
) -> GenerationRun:
    model_version = get_or_create_model_version(
        session,
        provider=settings.llm_provider,
        model_id=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )
    prompt_version = get_or_create_prompt_version(
        session,
        channel=channel,
        prompt_variant=state.get("prompt_variant") or "",
        angle=state.get("angle") or "",
    )

    run = GenerationRun(
        run_id=state["run_id"],
        trace_id=state.get("trace_id"),
        client_id=state["client_id"],
        product=state.get("product"),
        priority_tier=state.get("priority_tier"),
        data_date=state.get("data_date"),
        rule_version=state.get("rule_version"),
        angle_catalog_version=state.get("angle_catalog_version"),
        tier_contract_version=state.get("tier_contract_version"),
        voice_contract_version=state.get("voice_contract_version"),
        safety_policy_version=state.get("safety_policy_version"),
        output_policy_version=state.get("output_policy_version"),
        personalization_policy_version=state.get("personalization_policy_version"),
        prompt_version_id=prompt_version.prompt_version_id,
        model_version_id=model_version.model_version_id,
        status=state["status"],
        attempts=state.get("attempts", 0),
        failed_guardrail=state.get("failed_guardrail"),
        reason=state.get("reason"),
        ai_draft_content=state.get("raw_structured_output"),
        context_payload=state.get("context"),
    )
    session.add(run)
    session.flush()
    return run


def get_or_create_rubric_version(session: Session) -> RubricVersion:
    text = rubric_text()
    rubric_hash = _hash(text)
    existing = session.scalar(select(RubricVersion).where(RubricVersion.rubric_hash == rubric_hash))
    if existing is not None:
        return existing

    row = RubricVersion(rubric_text=text, rubric_hash=rubric_hash)
    session.add(row)
    session.flush()
    return row


def persist_evaluation(
    session: Session,
    run: GenerationRun,
    scores: EvaluationScores,
    settings: Settings,
) -> Evaluation:
    rubric_version = get_or_create_rubric_version(session)
    provider, model_id, temperature, max_tokens = resolve_judge_model_config(settings)
    model_version = get_or_create_model_version(
        session,
        provider=provider,
        model_id=model_id,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    row = Evaluation(
        run_id=run.run_id,
        rubric_version_id=rubric_version.rubric_version_id,
        model_version_id=model_version.model_version_id,
        tone=scores.tone,
        compliance=scores.compliance,
        grounding=scores.grounding,
        personalization=scores.personalization,
        notes=scores.notes,
    )
    session.add(row)
    session.flush()
    return row
