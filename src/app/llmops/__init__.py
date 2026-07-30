"""Observability for model calls: tracing, evaluation, and prompt and model versioning."""

from app.llmops.judge import build_judge_prompt, judge_draft, rubric_text
from app.llmops.telemetry import persist_generation_telemetry
from app.llmops.tracing import LangfuseTracer, NullTracer, Tracer, get_tracer
from app.llmops.versions import (
    EMAIL_CHANNEL,
    get_or_create_model_version,
    get_or_create_prompt_version,
    get_or_create_rubric_version,
    persist_evaluation,
    persist_generation_run,
)

__all__ = [
    "EMAIL_CHANNEL",
    "LangfuseTracer",
    "NullTracer",
    "Tracer",
    "build_judge_prompt",
    "get_or_create_model_version",
    "get_or_create_prompt_version",
    "get_or_create_rubric_version",
    "get_tracer",
    "judge_draft",
    "persist_evaluation",
    "persist_generation_run",
    "persist_generation_telemetry",
    "rubric_text",
]
