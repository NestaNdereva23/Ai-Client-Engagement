"""One drafted template per bucket, reviewed once and instantiated per client.

A bucket is a group of clients who share the same profile-defining facts:
the angle, tier, and product that decide what a message may claim, plus the
handful of booleans that add or drop a prohibition. message_template holds
the one draft written for that shared profile; outreach_message rows filled
from it (one per client in the bucket) point back to it through template_id.
A message drafted the old, per-client way has no template at all.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MESSAGE_TEMPLATE_STATUSES = ("pending_review", "approved", "rejected", "escalated", "held")


class MessageTemplate(Base):
    """One bucket's drafted template: the shared profile plus the placeholder draft."""

    __tablename__ = "message_template"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'escalated', 'held')",
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
    # tier, product, and the conditional-prohibition booleans from
    # app.agents.email_agent.conditional_prohibitions. Not one client's
    # facts, the shape a client has to match to be filled from this template.
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
