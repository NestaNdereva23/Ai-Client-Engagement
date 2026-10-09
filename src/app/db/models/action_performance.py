from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

NONE_LABEL = "none"
UNKNOWN_LABEL = "unknown"


class ActionPerformance(Base):
    __tablename__ = "action_performance"
    __table_args__ = (
        UniqueConstraint(
            "period_start",
            "period_hours",
            "window_days",
            "action_code",
            "angle",
            "priority_tier",
            "risk_band",
            "content_mix",
            "variant",
            name="uq_action_performance_period_key",
        ),
        CheckConstraint("period_hours > 0", name="ck_action_performance_period_hours_positive"),
        CheckConstraint("window_days > 0", name="ck_action_performance_window_days_positive"),
        CheckConstraint("sent_count > 0", name="ck_action_performance_sent_count_positive"),
        CheckConstraint("money_in_kes >= 0", name="ck_action_performance_money_not_negative"),
        Index("ix_action_performance_read", "period_hours", "window_days", "period_start"),
    )

    performance_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    action_code: Mapped[str] = mapped_column(Text, nullable=False)
    angle: Mapped[str] = mapped_column(Text, nullable=False)
    priority_tier: Mapped[str] = mapped_column(Text, nullable=False)
    risk_band: Mapped[str] = mapped_column(Text, nullable=False)
    content_mix: Mapped[str] = mapped_column(Text, nullable=False)
    variant: Mapped[str] = mapped_column(Text, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False)
    replied_count: Mapped[int] = mapped_column(Integer, nullable=False)
    opted_out_count: Mapped[int] = mapped_column(Integer, nullable=False)
    edited_count: Mapped[int] = mapped_column(Integer, nullable=False)
    deposited_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reply_rate: Mapped[float] = mapped_column(Float, nullable=False)
    opt_out_rate: Mapped[float] = mapped_column(Float, nullable=False)
    edit_rate: Mapped[float] = mapped_column(Float, nullable=False)
    deposit_rate: Mapped[float] = mapped_column(Float, nullable=False)
    money_in_kes: Mapped[float] = mapped_column(Float, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
