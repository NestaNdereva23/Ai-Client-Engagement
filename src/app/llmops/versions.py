"""Register prompt and model versions, and stamp them on a generation run.

Both registries are get or create by content hash: registering an unchanged
(model, temperature, max_tokens) tuple or an unchanged prompt template reuses
the existing row; a genuine change writes a new one. This module is the only
place a GenerationState gets persisted, deliberately kept outside
agents.graph so the graph itself never needs a database to be unit tested.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.email_agent import template_text
from app.config import Settings
from app.db.models.llmops import GenerationRun, ModelVersion, PromptVersion

# The only channel today; a future SMS/WhatsApp agent registers its own.
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
    """Look up the (provider, model, temperature, max_tokens) tuple, or register it."""
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
    """Look up the rendered instruction template for this variant, or register it."""
    text = template_text(prompt_variant or None)
    template_hash = _hash(text)
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
    """Stamp a terminal GenerationState with its prompt and model version, and store it.

    state must be terminal (status "accepted" or "rejected"); settings is the
    same config that built the LLMClient the run used, so the stamped model
    version always matches what actually generated the draft. ai_draft_content
    is state["raw_structured_output"] unchanged, which is null for a run that
    never reached structured-output parsing (a pii_scan leak or malformed
    JSON).
    """
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
        prompt_version_id=prompt_version.prompt_version_id,
        model_version_id=model_version.model_version_id,
        status=state["status"],
        attempts=state.get("attempts", 0),
        failed_guardrail=state.get("failed_guardrail"),
        reason=state.get("reason"),
        ai_draft_content=state.get("raw_structured_output"),
    )
    session.add(run)
    session.flush()
    return run
