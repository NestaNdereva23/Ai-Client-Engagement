from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

PATTERN_STATUSES = ("new", "worth_a_group_filter", "set_aside")
PATTERN_DIRECTIONS = ("better", "worse")
PATTERN_OUTCOMES = ("deposited", "replied")
PATTERN_COMPARISONS = ("other_messages", "not_messaged")


class ObservedPattern(Base):
    __tablename__ = "observed_pattern"
    __table_args__ = (
        UniqueConstraint("pattern_key", name="uq_observed_pattern_key"),
        CheckConstraint(
            "status IN ('new', 'worth_a_group_filter', 'set_aside')",
            name="ck_observed_pattern_status",
        ),
        CheckConstraint("direction IN ('better', 'worse')", name="ck_observed_pattern_direction"),
        CheckConstraint("outcome IN ('deposited', 'replied')", name="ck_observed_pattern_outcome"),
        CheckConstraint(
            "compared_with IN ('other_messages', 'not_messaged')",
            name="ck_observed_pattern_compared_with",
        ),
        CheckConstraint("window_days > 0", name="ck_observed_pattern_window_days_positive"),
        CheckConstraint("sent_count > 0", name="ck_observed_pattern_sent_count_positive"),
        CheckConstraint("comparison_count > 0", name="ck_observed_pattern_comparison_positive"),
        CheckConstraint(
            "(reviewed_by IS NULL) = (reviewed_at IS NULL)",
            name="ck_observed_pattern_review_pair",
        ),
    )

    pattern_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pattern_key: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    features: Mapped[dict] = mapped_column(JSONB, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    compared_with: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome_percent: Mapped[float] = mapped_column(Float, nullable=False)
    comparison_percent: Mapped[float] = mapped_column(Float, nullable=False)
    gap_points: Mapped[float] = mapped_column(Float, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False)
    comparison_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="new")
    insight_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent_insight.insight_id"), nullable=True, index=True
    )
    found_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
