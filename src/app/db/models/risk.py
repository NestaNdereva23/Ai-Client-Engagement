"""Risk scoring tables: risk_config_version, client_risk_features,
risk_run, and risk_snapshot.
"""

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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Allowed values for risk_run.state, same three states ingestion_status uses.
RISK_RUN_STATES = ("running", "completed", "failed")


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
    risk_score, risk_band, risk_reasons, aum_at_risk, credible_rhythm,
    lapse_ratio, recency_band, balance_tier, and value_tier -- everything
    compose_score produces from an ActiveFeatureMeasures row and the active
    config. route and queue_rank are the routing milestone's job and stay
    null until then.
    """

    __tablename__ = "client_risk_features"

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # Nullable because a nightly run hasn't written this row yet, not because
    # the bucketing is undecided -- see transform/active_features.py for the
    # frozen cutoffs.
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

    # risk/routing.py's output (AM7). Stays null here until the nightly job
    # (AM9) writes this table.
    route: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class RiskRun(Base):
    """One nightly detection run.

    Mirrors ingestion_status's shape (run_id, state, started_at,
    reference_ts, finished_at): same three states, same resumability
    discipline, so a run's own health is checked the same way. reference_ts
    is set once when the run starts and never changed, so every days_since_*
    figure risk_snapshot carries for this run is anchored consistently.
    """

    __tablename__ = "risk_run"
    __table_args__ = (
        CheckConstraint("state IN ('running', 'completed', 'failed')", name="ck_risk_run_state"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, autoincrement=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reference_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Which config version this run scored and routed against.
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)


class RiskSnapshot(Base):
    """One client-fund's numbers as of one run, append-only.

    Carries the same numbers as client_risk_features at the moment that run
    computed them. Never updated after it is written -- a repeat write for
    the same run_id/client_id/unit_fund_id hits uq_risk_snapshot_run_client_fund
    and raises, rather than silently overwriting history. This is the
    source of truth a day-over-day delta is computed from; client_risk_features
    is a disposable cache of its most recent row.
    """

    __tablename__ = "risk_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "client_id", "unit_fund_id", name="uq_risk_snapshot_run_client_fund"
        ),
        # Lookup order for latest_snapshot_for/delta_for: every prior
        # snapshot for one client-fund, across runs.
        Index("ix_risk_snapshot_client_fund_run", "client_id", "unit_fund_id", "run_id"),
    )

    snapshot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("risk_run.run_id"), nullable=False)
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

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
    risk_reasons: Mapped[str] = mapped_column(Text, nullable=False)
    aum_at_risk: Mapped[float] = mapped_column(Float, nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)

    route: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
