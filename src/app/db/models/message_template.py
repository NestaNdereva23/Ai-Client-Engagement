"""One drafted template per bucket, reviewed once and instantiated per client.

outreach_message rows filled from a template point back to it through
template_id. A message drafted the old, per-client way has no template.

message_template_review_action is its own table, not a reuse of
review_action: a template decision is a content judgment, mandatory for
every tier; an instantiated message's own review_action is a separate,
sampled, mostly substitution-correctness check.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MESSAGE_TEMPLATE_STATUSES = (
    "pending_review",
    "approved",
    "rejected",
    "escalated",
    "held",
    "guardrail_rejected",
)
TEMPLATE_REVIEW_OUTCOMES = ("approve", "edit_approve", "reject", "escalate", "hold")


class MessageTemplate(Base):
    """One bucket's drafted template: the shared profile plus the placeholder draft."""

    __tablename__ = "message_template"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'escalated', 'held', "
            "'guardrail_rejected')",
            name="ck_message_template_status",
        ),
    )

    template_id: Mapped[str] = mapped_column(Text, primary_key=True, autoincrement=False)
    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaign.campaign_id"), nullable=False, index=True
    )
    generation_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("generation_runs.run_id"), nullable=False, unique=True
    )
    # The profile-defining facts every client in the bucket shares: angle,
    # tier, product, and the conditional-prohibition booleans.
    profile_key: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ai_draft_content: Mapped[dict] = mapped_column(JSONB, nullable=False)
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


class TemplateReviewAction(Base):
    """One reviewer decision on one template.

    message_angle/priority_tier are stamped from the template's own
    profile_key at decide time, so the label survives later catalogue changes.
    """

    __tablename__ = "message_template_review_action"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('approve', 'edit_approve', 'reject', 'escalate', 'hold')",
            name="ck_message_template_review_action_outcome",
        ),
    )

    review_action_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    template_id: Mapped[str] = mapped_column(
        Text, ForeignKey("message_template.template_id"), nullable=False, index=True
    )
    reviewer_id: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    edited_content: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    message_angle: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    priority_tier: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    edit_diff: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
