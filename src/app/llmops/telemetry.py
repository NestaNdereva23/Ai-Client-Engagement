"""Persist a run's llm_calls/tool_calls into their tables, plus a trace_refs row."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.db.models.llmops import (
    GenerationRun,
    LLMRequest,
    LLMResponse,
    TokenUsage,
    ToolCall,
    TraceRef,
)
from app.llmops.tracing import Tracer


def persist_generation_telemetry(
    session: Session,
    run: GenerationRun,
    state: Mapping[str, Any],
    *,
    tracer: Tracer | None = None,
) -> None:
    """Write one row per llm_calls/tool_calls entry, plus a trace_refs row, for run."""
    for call in state.get("llm_calls", ()):
        request = LLMRequest(
            run_id=run.run_id,
            attempt=call["attempt"],
            model_version_id=run.model_version_id,
            system_prompt=call["system_prompt"],
        )
        session.add(request)
        session.flush()
        session.add(
            LLMResponse(
                request_id=request.request_id,
                raw_output=call["raw_output"],
                latency_ms=call["latency_ms"],
            )
        )
        session.add(
            TokenUsage(
                request_id=request.request_id,
                input_tokens=call["input_tokens"],
                output_tokens=call["output_tokens"],
            )
        )

    for tool_call in state.get("tool_calls", ()):
        session.add(
            ToolCall(
                run_id=run.run_id,
                tool_name=tool_call["tool_name"],
                tool_input=tool_call["input"],
                tool_output=tool_call["output"],
            )
        )

    if run.trace_id:
        trace_url = tracer.get_trace_url(run.trace_id) if tracer else None
        session.add(TraceRef(run_id=run.run_id, trace_id=run.trace_id, trace_url=trace_url))

    session.flush()
