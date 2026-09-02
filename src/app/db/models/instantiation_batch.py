from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

INSTANTIATION_BATCH_STATUSES = ("running", "completed", "no_approved_templates", "failed")


class InstantiationBatch(Base):
    __tablename__ = "instantiation_batch"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'no_approved_templates', 'failed')",
            name="ck_instantiation_batch_status",
        ),
    )

    instantiation_batch_id: Mapped[str] = mapped_column(Text, primary_key=True, autoincrement=False)
    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaign.campaign_id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    template_count: Mapped[int] = mapped_column(Integer, nullable=False)
    instantiated_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_template_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
