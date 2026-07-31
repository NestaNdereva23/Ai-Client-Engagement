"""Campaign orchestration: campaign_step, enrollment, touch_log, contact_events."""

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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

ENROLLMENT_STATUSES = (
    "enrolled",
    "in_progress",
    "excluded",
    "completed",
    "stopped_reply",
    "stopped_optout",
    "stopped_bounce",
    "stopped_reengaged",
)
CONTACT_EVENT_TYPES = ("reply", "open", "bounce", "complaint", "optout")


class CampaignStep(Base):
    __tablename__ = "campaign_step"
    __table_args__ = (UniqueConstraint("campaign_id", "step_no", name="uq_campaign_step_no"),)

    step_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaign.campaign_id"), nullable=False, index=True
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    offset_days: Mapped[int] = mapped_column(Integer, nullable=False)
    message_angle: Mapped[str] = mapped_column(Text, nullable=False)
    template_ref: Mapped[str | None] = mapped_column(Text, nullable=True)


class Enrollment(Base):
    """One row per client per campaign; current_step/next_due_at drive the scheduler."""

    __tablename__ = "enrollment"
    __table_args__ = (
        CheckConstraint(
            "status IN ('enrolled', 'in_progress', 'excluded', 'completed', "
            "'stopped_reply', 'stopped_optout', 'stopped_bounce', 'stopped_reengaged')",
            name="ck_enrollment_status",
        ),
        UniqueConstraint("campaign_id", "client_id", name="uq_enrollment_campaign_client"),
    )

    enrollment_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaign.campaign_id"), nullable=False, index=True
    )
    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.client_id"), nullable=False, index=True
    )
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="enrolled")
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TouchLog(Base):
    """Every generated/sent touch; the source of truth for "already received"."""

    __tablename__ = "touch_log"
    __table_args__ = (
        UniqueConstraint("enrollment_id", "step_no", name="uq_touch_log_enrollment_step"),
    )

    touch_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    enrollment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("enrollment.enrollment_id"), nullable=False, index=True
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("outreach_message.message_id"), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ContactEvent(Base):
    """Inbound signal (reply/open/bounce/complaint/optout) feeding the eligibility gate."""

    __tablename__ = "contact_events"
    __table_args__ = (
        CheckConstraint(
            "type IN ('reply', 'open', 'bounce', 'complaint', 'optout')",
            name="ck_contact_events_type",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
