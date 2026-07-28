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
