"""The complaints stub source: client_complaint.

Holds no free-text complaint body: only the fact that a complaint exists,
when, and its category and channel, is enough for the routing override and
for a briefing to say "there is an open complaint" without the system ever
reading or reasoning about what it says.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

COMPLAINT_STATUSES = ("open", "closed")
COMPLAINT_CATEGORIES = ("billing", "service", "product", "other")
COMPLAINT_CHANNELS = ("call", "email", "branch", "other")


class ClientComplaint(Base):
    """One complaint against a client, from whichever source is configured."""

    __tablename__ = "client_complaint"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed')", name="ck_client_complaint_status"),
        CheckConstraint(
            "category IN ('billing', 'service', 'product', 'other')",
            name="ck_client_complaint_category",
        ),
        CheckConstraint(
            "channel IN ('call', 'email', 'branch', 'other')",
            name="ck_client_complaint_channel",
        ),
        Index("ix_client_complaint_client_id", "client_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    opened_at: Mapped[date] = mapped_column(Date, nullable=False)
    closed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    # "stub" today; a real source name once one exists.
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="stub")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
