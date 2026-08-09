"""Batch generation tracking: one row per submission to the provider's async
batch endpoint, and one row per client request bundled into it.

Submitting a batch and ingesting its results are two separate events, since
the provider's batch can take up to a day to finish. generation_batch tracks
the submission itself; generation_batch_item snapshots everything one
client's request needs so ingestion can turn a result into a generation_runs
row without re-deriving context that may have moved on by the time the
result comes back.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

GENERATION_BATCH_STATUSES = (
    "building",
    "no_eligible_clients",
    "submitted",
    "in_progress",
    "ended",
    "ingested",
    "failed",
)
GENERATION_BATCH_ITEM_STATUSES = ("pending", "accepted", "rejected")


class GenerationBatch(Base):
    """One submission to the provider's async batch endpoint, for one campaign."""

    __tablename__ = "generation_batch"
    __table_args__ = (
        CheckConstraint(
            "status IN ('building', 'no_eligible_clients', 'submitted', 'in_progress', "
            "'ended', 'ingested', 'failed')",
            name="ck_generation_batch_status",
        ),
    )

    generation_batch_id: Mapped[str] = mapped_column(Text, primary_key=True, autoincrement=False)
    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaign.campaign_id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_batch_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="building")
    # How many clients this submission could include, and how many actually
    # went out; requested_count can be lower than the limit when fewer
    # enrollments were due and eligible than the cap allowed.
    requested_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    succeeded_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    errored_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GenerationBatchItem(Base):
    """One client's request within a batch, with everything ingestion needs
    to turn its result into a generation run without re-deriving context.
    """

    __tablename__ = "generation_batch_item"
    __table_args__ = (
        UniqueConstraint(
            "generation_batch_id", "custom_id", name="uq_generation_batch_item_custom_id"
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name="ck_generation_batch_item_status",
        ),
    )

    generation_batch_item_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    generation_batch_id: Mapped[str] = mapped_column(
        Text, ForeignKey("generation_batch.generation_batch_id"), nullable=False, index=True
    )
    # The id sent to the provider as custom_id, and the run_id the result is
    # eventually persisted under -- the same value, so ingestion never needs
    # a separate lookup table to reconnect a result to the request that
    # produced it.
    custom_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.client_id"), nullable=False, index=True
    )
    enrollment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("enrollment.enrollment_id"), nullable=False, index=True
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Everything the synchronous path's retrieve_context/assemble_prompt
    # steps produce for this client, captured at submit time: angle, tier,
    # rule/catalogue versions, facts, chunks, the rendered system prompt.
    # Ingestion reads this instead of re-resolving the client, so a result
    # that comes back a day later is still judged against the exact facts
    # it was drafted from, not whatever the client looks like by then.
    context_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
