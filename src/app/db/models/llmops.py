"""The prompt/model version registry and the generation runs stamped with them.

prompt_versions and model_versions are content addressed: a version is
identified by a hash of what it actually contains (the template text, or the
provider/model/temperature/max_tokens tuple), so calling with an unchanged
config reuses the same row and a genuine change registers a new one. There is
no time-windowed validity here, unlike business_rules; version identity is
the content itself.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ModelVersion(Base):
    """One distinct (provider, model, temperature, max_tokens) configuration."""

    __tablename__ = "model_versions"

    model_version_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PromptVersion(Base):
    """One distinct rendered instruction template for a channel and prompt variant."""

    __tablename__ = "prompt_versions"

    prompt_version_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_variant: Mapped[str] = mapped_column(Text, nullable=False)
    angle: Mapped[str] = mapped_column(Text, nullable=False)
    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    template_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GenerationRun(Base):
    """One terminal draft-generation run, stamped with the versions that produced it."""

    __tablename__ = "generation_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True, autoincrement=False)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.client_id"), nullable=False, index=True
    )
    product: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("prompt_versions.prompt_version_id"), nullable=False
    )
    model_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("model_versions.model_version_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)  # "accepted" | "rejected"
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_guardrail: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The model's structured output exactly as generated, pre re-attachment;
    # null when a run never reached structured-output parsing (an inbound
    # boundary leak never reaches here at all, an outbound leak or malformed
    # JSON leaves no validated content to store).
    ai_draft_content: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LLMRequest(Base):
    """One model call attempt within a generation run."""

    __tablename__ = "llm_requests"

    request_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("generation_runs.run_id"), nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("model_versions.model_version_id"), nullable=False
    )
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LLMResponse(Base):
    """The reply for one llm_requests row; raw_output is null for a pii_scan block."""

    __tablename__ = "llm_responses"

    response_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("llm_requests.request_id"), nullable=False, unique=True
    )
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TokenUsage(Base):
    """Token counts for one llm_requests row; null when the client didn't report usage."""

    __tablename__ = "token_usage"

    token_usage_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("llm_requests.request_id"), nullable=False, unique=True
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolCall(Base):
    """One non-LLM tool invocation (context fetch, RAG retrieval) within a run."""

    __tablename__ = "tool_calls"

    tool_call_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("generation_runs.run_id"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    tool_input: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tool_output: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TraceRef(Base):
    """The Langfuse trace id (and resolved URL, when available) for one run."""

    __tablename__ = "trace_refs"

    run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("generation_runs.run_id"), primary_key=True, autoincrement=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    trace_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
