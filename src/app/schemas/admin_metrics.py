"""Response shapes for the admin metrics endpoints."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RunMetricsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    message_angle: str | None
    priority_tier: str | None
    prompt_variant: str | None
    model_id: str | None
    run_count: int
    error_rate: float
    avg_latency_ms: float | None
    avg_input_tokens: float | None
    avg_output_tokens: float | None
    avg_cost_usd: float | None


class GuardrailFailureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    message_angle: str | None
    failed_guardrail: str
    fail_count: int
    run_count: int
    failure_rate: float
