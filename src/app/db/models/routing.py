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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ClientSide(Base):
    __tablename__ = "client_side"
    __table_args__ = (
        CheckConstraint("side in ('active', 'inactive')", name="ck_client_side_side"),
        CheckConstraint(
            "previous_side in ('active', 'inactive')", name="ck_client_side_previous_side"
        ),
    )

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    balance: Mapped[float] = mapped_column(Float, nullable=False)
    threshold_kes: Mapped[float] = mapped_column(Float, nullable=False)
    source_feed: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    previous_side: Mapped[str | None] = mapped_column(Text, nullable=True)
    side_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClientTierThresholds(Base):
    __tablename__ = "client_tier_thresholds"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    review_period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    recent_purchase_days: Mapped[int] = mapped_column(Integer, nullable=False)
    capital_preservation_threshold_kes: Mapped[float] = mapped_column(Float, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ClientTier(Base):
    __tablename__ = "client_tier"
    __table_args__ = (
        CheckConstraint(
            "tier in ('gradual_depositor', 'gradual_depositor_withdrawer', 'frequent_depositor', "
            "'frequent_depositor_withdrawer', 'no_transactions_in_review_period', "
            "'below_capital_preservation_threshold')",
            name="ck_client_tier_tier",
        ),
        CheckConstraint(
            "segment in ('active_active', 'active_dormant')", name="ck_client_tier_segment"
        ),
    )

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    purchased_recently: Mapped[bool] = mapped_column(Boolean, nullable=False)
    real_withdrawer: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tier: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    segment: Mapped[str | None] = mapped_column(Text, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    thresholds_version: Mapped[int] = mapped_column(
        Integer, ForeignKey("client_tier_thresholds.version"), nullable=False
    )
    run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    tier_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
