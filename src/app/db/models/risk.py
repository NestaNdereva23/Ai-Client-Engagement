"""Risk scoring tables: risk_config_version and client_risk_features now,
risk_run and risk_snapshot land with the nightly detection job.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RiskConfigVersion(Base):
    """One immutable set of signal weights and thresholds.

    Versioned exactly like message_angle_catalog and tier_contract: a shipped
    version is never edited, so a score computed last month can still be
    explained against the exact constants that produced it. Retuning means
    saving a new version with a new valid_from.
    """

    __tablename__ = "risk_config_version"

    config_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    # One weight per signal name (sig_drawdown, sig_dormant, ...), summing to 100.
    weights: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # DORMANT_DAYS, DRAWDOWN_HEAVY, LAPSE_MULTIPLE, DECLINE_SLOPE, DUST_BALANCE,
    # MATERIAL_BALANCE, FEE_RUNWAY_MONTHS, FEE_PER_MONTH, SYSTEM_SALE_MAX, and
    # RISK_BAND_CUTOFFS (four ascending cutoffs for None/Low/Watch/High/Critical).
    thresholds: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fa_call_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    at_risk_min: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ClientRiskFeatures(Base):
    """The current-state risk row for one client-fund relationship.

    Recomputed every nightly run and upserted here; this is always the
    latest run's numbers, never a history (risk_snapshot, landing with the
    nightly job, is the append-only history this table is a disposable
    cache of the most recent row of).

    Columns this milestone (score composition) fills: the six sig_* signals,
    risk_score, risk_band, risk_reasons, aum_at_risk, credible_rhythm, and
    lapse_ratio -- everything compose_score produces from an ActiveFeatureMeasures
    row and the active config. recency_band, balance_tier, and value_tier need
    their own frozen cutoffs, the same kind of decision Phase 1's value_band
    needed, and stay null until that decision is made. route and queue_rank
    are the routing milestone's job and stay null until then.
    """

    __tablename__ = "client_risk_features"

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # Deferred bucketing -- see the class docstring.
    recency_band: Mapped[str | None] = mapped_column(Text, nullable=True)
    balance_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_tier: Mapped[str | None] = mapped_column(Text, nullable=True)

    credible_rhythm: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    lapse_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)

    sig_drawdown: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sig_dormant: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sig_cadence_break: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sig_shrinking: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sig_fee_erosion: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sig_never_repeated: Mapped[bool] = mapped_column(Boolean, nullable=False)

    risk_score: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_band: Mapped[str] = mapped_column(Text, nullable=False)
    # Never shipped without the score: joined labels of fired signals, or
    # "no signal" when none fired.
    risk_reasons: Mapped[str] = mapped_column(Text, nullable=False)
    aum_at_risk: Mapped[float] = mapped_column(Float, nullable=False)

    # Which config version produced this row, so a score is always
    # explainable against the exact constants that made it.
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)

    # The routing milestone's output -- see the class docstring.
    route: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
