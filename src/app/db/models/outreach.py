"""The outreach workflow: campaign, outreach_message, review_action.

An accepted generation run becomes one or more outreach_message rows, each
holding what the model saw (ai_draft_content) and what re-attachment
produces (personalized_content). A run behind a message_template backs
many messages, all sharing template_id.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

CAMPAIGN_STATUSES = ("draft", "running", "paused", "completed")
MESSAGE_STATUSES = ("pending_review", "approved", "rejected", "escalated", "held")
REVIEW_OUTCOMES = ("approve", "edit_approve", "reject", "escalate", "hold")
COHORT_STATUSES = ("sampling", "ready_to_approve_rest", "completed")


class Campaign(Base):
    __tablename__ = "campaign"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'running', 'paused', 'completed')", name="ck_campaign_status"
        ),
    )

    campaign_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    campaign_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="dormant_reengagement"
    )
    # Allow-listed feature values (segment, tier, bucket) the cohort was selected
    # on, not a free-text description, so enrollment can re-derive membership.
    cohort_definition: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReviewCohort(Base):
    __tablename__ = "review_cohort"
    __table_args__ = (
        CheckConstraint(
            "status IN ('sampling', 'ready_to_approve_rest', 'completed')",
            name="ck_review_cohort_status",
        ),
    )

    cohort_id: Mapped[str] = mapped_column(Text, primary_key=True, autoincrement=False)
    # Cascades on campaign delete: nothing in the app ever deletes a
    # campaign (there is no delete endpoint), so this only ever fires in
    # tests tearing down a fixture campaign, sparing every test that
    # creates a single-generated message from also having to clean up the
    # cohort it implicitly created.
    campaign_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("campaign.campaign_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    priority_tier: Mapped[str] = mapped_column(Text, nullable=False)
    # Null rate means no sampling at all: every message is reviewed.
    sample_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assigned_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="sampling")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutreachMessage(Base):
    __tablename__ = "outreach_message"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'escalated', 'held')",
            name="ck_outreach_message_status",
        ),
    )

    message_id: Mapped[str] = mapped_column(Text, primary_key=True, autoincrement=False)
    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaign.campaign_id"), nullable=False, index=True
    )
    generation_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("generation_runs.run_id"), nullable=False
    )
    # Set only when this message was filled in from an approved
    # message_template rather than drafted individually.
    template_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("message_template.template_id"), nullable=True, index=True
    )
    # Set only for a single-generated message that belongs to a sampling
    # cohort (campaign x tier). Null for template-instantiated messages
    # and any single-generated message drafted before this existed.
    cohort_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("review_cohort.cohort_id", ondelete="SET NULL"), nullable=True, index=True
    )
    # True for a message chosen as one of its cohort's samples -- the ones
    # the default review queue shows. Always false outside a cohort.
    is_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.client_id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False, server_default="email")
    ai_draft_content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    personalized_content: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Set only for a tier whose contract adds a secondary call_brief channel
    # (today, T1). Carries no PII and needs no personalization.
    call_brief: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending_review", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ReviewAction(Base):
    __tablename__ = "review_action"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('approve', 'edit_approve', 'reject', 'escalate', 'hold')",
            name="ck_review_action_outcome",
        ),
    )

    review_action_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(
        Text, ForeignKey("outreach_message.message_id"), nullable=False, index=True
    )
    reviewer_id: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    edited_content: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Stamped from the message's generation run at decide() time, so this
    # decision stays a ground-truth label for the angle and tier it was
    # actually made under, even after the client's own indicators move on.
    message_angle: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    priority_tier: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    # Per-field unified diff between ai_draft_content and edited_content, set
    # only for edit_approve; what a reviewer changes is the signal for which
    # angle brief is weak, not just that they changed something.
    edit_diff: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
