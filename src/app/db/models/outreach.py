"""The outreach workflow: campaign, outreach_message, review_action.

An accepted generation run becomes one outreach_message, holding both what
the model saw (ai_draft_content) and what re-attachment produces
(personalized_content, filled in separately). review_action is the
append-only history of every human decision on a message; status is the
current state, kept in step with the latest action so the review queue can
filter without replaying history. campaign here is intentionally minimal,
just enough for outreach_message to group under; M9 extends it with real
cohort and scheduling columns.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

CAMPAIGN_STATUSES = ("draft", "running", "paused", "completed")
MESSAGE_STATUSES = ("pending_review", "approved", "rejected", "escalated", "held")
REVIEW_OUTCOMES = ("approve", "edit_approve", "reject", "escalate", "hold")


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
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


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
        Text, ForeignKey("generation_runs.run_id"), nullable=False, unique=True
    )
    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.client_id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False, server_default="email")
    ai_draft_content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    personalized_content: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
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
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
