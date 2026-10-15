from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    Time,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

FLAGGED_FOR_ACCOUNT_MANAGER = "flagged_for_account_manager"
AI_ACTION_TAKEN = "ai_action_taken"
AI_ACTION_SET_ASIDE = "ai_action_set_aside"
AI_ACTION_INTERACTION_TYPES = (AI_ACTION_TAKEN, AI_ACTION_SET_ASIDE)

CHANNEL_BY_INTERACTION_TYPE = {
    "call_logged": "call",
    "email_sent": "email",
    "meeting_logged": "meeting",
    "whatsapp_logged": "whatsapp",
}
CONTACT_INTERACTION_TYPES = tuple(CHANNEL_BY_INTERACTION_TYPE)
INTERACTION_OUTCOMES = ("connected", "follow_up", "no_answer")

INTERACTION_TYPES = (
    *CONTACT_INTERACTION_TYPES,
    "snoozed",
    "dismissed",
    FLAGGED_FOR_ACCOUNT_MANAGER,
    *AI_ACTION_INTERACTION_TYPES,
)

# A flag is a note asking someone to look, so it is deliberately not handled:
# a flagged client stays where the morning list can see it.
HANDLED_INTERACTION_TYPES = (
    *CONTACT_INTERACTION_TYPES,
    "snoozed",
    "dismissed",
    *AI_ACTION_INTERACTION_TYPES,
)

TOUCHPOINT_KINDS = ("call", "meeting", "email", "reminder")
TOUCHPOINT_OPEN = "open"
TOUCHPOINT_DONE = "done"
TOUCHPOINT_REMOVED = "removed"
TOUCHPOINT_STATUSES = (TOUCHPOINT_OPEN, TOUCHPOINT_DONE, TOUCHPOINT_REMOVED)


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
    first_deposit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
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
    fa_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    fa_email: Mapped[str | None] = mapped_column(Text, nullable=True)

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


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class ActiveClientInteraction(Base):
    __tablename__ = "active_client_interaction"
    __table_args__ = (
        CheckConstraint(
            f"type IN ({_quoted(INTERACTION_TYPES)})",
            name="ck_active_client_interaction_type",
        ),
        CheckConstraint(
            f"outcome IN ({_quoted(INTERACTION_OUTCOMES)})",
            name="ck_active_client_interaction_outcome",
        ),
        Index(
            "ix_active_client_interaction_client_fund", "client_id", "unit_fund_id", "created_at"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Set only when an admin logs this while viewing as an advisor.
    on_behalf_of: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_band_at_interaction: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent_proposal.proposal_id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ActiveClientTouchpoint(Base):
    __tablename__ = "active_client_touchpoint"
    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_quoted(TOUCHPOINT_KINDS)})", name="ck_active_client_touchpoint_kind"
        ),
        CheckConstraint(
            f"status IN ({_quoted(TOUCHPOINT_STATUSES)})",
            name="ck_active_client_touchpoint_status",
        ),
        Index(
            "ix_active_client_touchpoint_client_fund_status",
            "client_id",
            "unit_fund_id",
            "status",
            "due_date",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=TOUCHPOINT_OPEN)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    # Set only when an admin adds this while viewing as an advisor.
    on_behalf_of: Mapped[str | None] = mapped_column(Text, nullable=True)
    done_by_interaction_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("active_client_interaction.id", ondelete="SET NULL"),
        nullable=True,
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
