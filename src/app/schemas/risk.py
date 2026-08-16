"""Request and response shapes for the risk endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RiskCoverageOut(BaseModel):
    """The active book's size vs. how many of them the last completed
    nightly run actually scored.
    """

    model_config = ConfigDict(from_attributes=True)

    book_size: int
    scored_count: int
    as_of: datetime | None


class RiskBucketOut(BaseModel):
    """One count bucket, shared by every risk-analytics breakdown below."""

    key: str | None
    count: int


class RiskAnalyticsOut(BaseModel):
    """Coverage plus book-wide cuts: risk-band, route, balance-tier,
    value-tier, and recency-band distribution; how often each of the six
    signals fires; and which signal most often drives the score
    (primary_signal_distribution). Plus total_aum_at_risk, the book-wide sum
    -- unlike DigestGroupOut's own total_aum_at_risk, which is scoped to one
    FA/fund group, this one is summed across the whole active book.
    """

    model_config = ConfigDict(from_attributes=True)

    book_size: int
    scored_count: int
    as_of: datetime | None
    by_risk_band: list[RiskBucketOut]
    by_route: list[RiskBucketOut]
    by_balance_tier: list[RiskBucketOut]
    by_value_tier: list[RiskBucketOut]
    by_recency_band: list[RiskBucketOut]
    signal_frequency: list[RiskBucketOut]
    primary_signal_distribution: list[RiskBucketOut]
    total_aum_at_risk: float


class RiskTrendPointOut(BaseModel):
    """One completed nightly run's book-wide numbers, for a trend chart."""

    model_config = ConfigDict(from_attributes=True)

    run_id: str
    as_of: datetime | None
    by_risk_band: list[RiskBucketOut]
    total_aum_at_risk: float
    avg_risk_score: float


class RiskTrendOut(BaseModel):
    """The last N completed nightly runs' book-wide numbers, oldest first."""

    model_config = ConfigDict(from_attributes=True)

    points: list[RiskTrendPointOut]


class DustCleanupLineOut(BaseModel):
    """One client-fund currently routed to dust_cleanup."""

    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    balance_tier: str | None
    risk_score: int
    risk_band: str
    risk_reasons: str
    aum_at_risk: float
