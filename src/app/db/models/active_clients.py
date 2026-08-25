from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

INTERACTION_TYPES = ("call_logged", "snoozed", "dismissed", "email_sent")


class ActiveClientFund(Base):
    """One row per client-fund relationship in the active book."""

    __tablename__ = "active_client_fund"

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    client_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_deposits: Mapped[int] = mapped_column(Integer, nullable=False)
    n_withdrawals: Mapped[int] = mapped_column(Integer, nullable=False)
    last_deposit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_withdrawal_slot_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    deposit_count_capped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # True when the withdrawal window is full, so older withdrawals are
    # hidden -- the active-book counterpart of client_fund.history_censored.
    withdrawal_history_hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    computed_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Derived measures, computed by transform/active_features.py from the
    # client's own transactions. Null here means "not yet computed", not
    # "zero".
    typical_gap_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_deposit_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_deposit_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_deposit_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    deposit_trend: Mapped[float | None] = mapped_column(Float, nullable=True)
    # A genuine client-initiated withdrawal, filtered to exclude
    # system-driven withdrawals (the SYSTEM_FEE_MAX rule of thumb).
    largest_withdrawal: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The most recent date among real withdrawals, not among every
    # withdrawal slot -- see transform/active_features.py::_last_withdrawal_date.
    last_withdrawal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    months_until_empty: Mapped[float | None] = mapped_column(Float, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ActiveClientInteraction(Base):
    __tablename__ = "active_client_interaction"
    __table_args__ = (
        CheckConstraint(
            "type IN ('call_logged', 'snoozed', 'dismissed', 'email_sent')",
            name="ck_active_client_interaction_type",
        ),
        Index(
            "ix_active_client_interaction_client_fund", "client_id", "unit_fund_id", "created_at"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer_id: Mapped[str] = mapped_column(Text, nullable=False)
    risk_band_at_interaction: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ActiveTransaction(Base):
    __tablename__ = "active_transaction"
    __table_args__ = (
        Index("ix_active_transaction_client_fund", "client_id", "unit_fund_id", "txn_date"),
    )

    txn_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    txn_type: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fund_short_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    txn_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees_incurred: Mapped[float | None] = mapped_column(Float, nullable=True)
    sale_type: Mapped[str | None] = mapped_column(Text, nullable=True)
