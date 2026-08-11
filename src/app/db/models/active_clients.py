"""The active book: active_client_fund, the active-client counterpart of client_fund.

Kept separate from client_fund rather than merged into it, because the
dormant table's columns describe a relationship that has already ended; an
active relationship has no exit yet, so a shared schema would leave half its
columns meaningless on every row.

Ingestion (this milestone) fills the directly observed columns: balance,
purchase/sale counts and dates, censoring flags, computed_at. The derived
measures (rhythm_days, ticket_trend, and the rest) stay null until the
active-book feature derivation milestone computes them.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Integer, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ActiveClientFund(Base):
    """One row per client-fund relationship in the active book."""

    __tablename__ = "active_client_fund"

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    client_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_purchases: Mapped[int] = mapped_column(Integer, nullable=False)
    n_sales: Mapped[int] = mapped_column(Integer, nullable=False)
    last_purchase: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_sale: Mapped[date | None] = mapped_column(Date, nullable=True)
    purchases_censored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # True when the sale window is full, so older redemptions are hidden --
    # the active-book counterpart of client_fund.history_censored.
    redemption_history_blind: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    computed_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Derived measures, filled in by the active-book feature derivation
    # milestone. Null here means "not yet computed", not "zero".
    rhythm_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_ticket: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_ticket: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_ticket: Mapped[float | None] = mapped_column(Float, nullable=True)
    ticket_trend: Mapped[float | None] = mapped_column(Float, nullable=True)
    # A genuine client-initiated redemption, filtered to exclude system-driven
    # sales (the SYSTEM_SALE_MAX heuristic).
    largest_real_sale: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_runway_months: Mapped[float | None] = mapped_column(Float, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
