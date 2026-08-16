"""Read-only views over the current risk state.

Everything here reads client_risk_features, the always-current cache of the
latest nightly run's numbers (see db/models/risk.py's own docstring) --
never risk_snapshot, since these are "what does ops need to act on right
now" reads, not a history read.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from app.db.models.active_clients import ActiveClientFund
from app.db.models.risk import ClientRiskFeatures, RiskConfigVersion, RiskRun, RiskSnapshot
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_pair_cursor, encode_pair_cursor
from app.risk.magnitude import pick_primary_signal
from app.risk.signals import SIGNAL_ORDER

DUST_CLEANUP_ROUTE = "dust_cleanup"


def list_dust_cleanup_queue(
    session: Session, *, cursor: str | None = None, limit: int = DEFAULT_LIMIT
) -> tuple[list[ClientRiskFeatures], str | None]:
    """The current dust_cleanup population, ordered by (client_id,
    unit_fund_id).

    Read-only, on purpose: ops takes a waive/notify/close decision manually,
    outside this codebase (Section 20 of the implementation plan). No
    campaigns/ module, enrollment path, or digest reads this route -- a
    client routed here can never reach an outreach campaign through any
    path this codebase provides.
    """
    limit = clamp_limit(limit)
    query = select(ClientRiskFeatures).where(ClientRiskFeatures.route == DUST_CLEANUP_ROUTE)
    if cursor is not None:
        after_client_id, after_unit_fund_id = decode_pair_cursor(cursor)
        query = query.where(
            tuple_(ClientRiskFeatures.client_id, ClientRiskFeatures.unit_fund_id)
            > (after_client_id, after_unit_fund_id)
        )
    query = query.order_by(ClientRiskFeatures.client_id, ClientRiskFeatures.unit_fund_id).limit(
        limit + 1
    )

    rows = list(session.scalars(query).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_pair_cursor(rows[-1].client_id, rows[-1].unit_fund_id)
    return rows, next_cursor


@dataclass(frozen=True)
class CoverageView:
    """book_size vs. how many of them the last completed nightly run scored."""

    book_size: int
    scored_count: int
    as_of: datetime | None


def book_coverage(session: Session) -> CoverageView:
    """How much of the active book the most recent completed nightly run
    actually scored.

    book_size is every client-fund relationship active_client_fund
    currently holds -- the whole active book as ingestion last saw it.
    scored_count is how many of those the latest completed risk_run wrote a
    risk_snapshot row for; as_of is that run's finished_at. A run still
    "running" never counts here, only a completed one -- the same
    completed-only reads risk_detection.py's own resume logic relies on.
    With no completed run yet at all, scored_count is 0 and as_of is None,
    not a guess.
    """
    book_size = session.scalar(select(func.count()).select_from(ActiveClientFund)) or 0

    latest_run = session.scalar(
        select(RiskRun)
        .where(RiskRun.state == "completed")
        .order_by(RiskRun.finished_at.desc(), RiskRun.started_at.desc())
        .limit(1)
    )
    if latest_run is None:
        return CoverageView(book_size=book_size, scored_count=0, as_of=None)

    scored_count = (
        session.scalar(
            select(func.count())
            .select_from(RiskSnapshot)
            .where(RiskSnapshot.run_id == latest_run.run_id)
        )
        or 0
    )
    return CoverageView(
        book_size=book_size, scored_count=scored_count, as_of=latest_run.finished_at
    )


@dataclass(frozen=True)
class RiskAnalytics:
    """Coverage plus book-wide cuts over client_risk_features: band, route,
    balance-tier, value-tier, and recency-band distribution, how often each
    signal fires, and which signal most often drives the score -- the
    candidate cuts the active-book analytics page was scoped around. Plus
    total_aum_at_risk, the book-wide sum of the same column
    DigestGroupData.total_aum_at_risk sums per FA/fund group.
    """

    book_size: int
    scored_count: int
    as_of: datetime | None
    by_risk_band: list[tuple[str, int]]
    by_route: list[tuple[str | None, int]]
    by_balance_tier: list[tuple[str | None, int]]
    by_value_tier: list[tuple[str | None, int]]
    by_recency_band: list[tuple[str | None, int]]
    signal_frequency: list[tuple[str, int]]
    primary_signal_distribution: list[tuple[str, int]]
    total_aum_at_risk: float


def _primary_signal_distribution(session: Session) -> list[tuple[str, int]]:
    """How often each signal is the one that actually drove a client's
    score -- not just fired (signal_frequency already covers that), but the
    single heaviest-weighted fired signal per app.risk.magnitude's own pick.

    Config versions are fetched once and reused across rows, since many
    clients share the same version rather than each earning its own query.
    """
    rows = session.execute(
        select(
            ClientRiskFeatures.config_version,
            *(getattr(ClientRiskFeatures, name) for name in SIGNAL_ORDER),
        )
    ).all()
    if not rows:
        return [(name.removeprefix("sig_"), 0) for name in SIGNAL_ORDER]

    versions = {row[0] for row in rows}
    weights_by_version = dict(
        session.execute(
            select(RiskConfigVersion.version, RiskConfigVersion.weights).where(
                RiskConfigVersion.version.in_(versions)
            )
        ).all()
    )

    tally: Counter[str] = Counter()
    for config_version, *flags in rows:
        signals = dict(zip(SIGNAL_ORDER, flags, strict=True))
        primary = pick_primary_signal(signals, weights_by_version.get(config_version, {}))
        if primary is not None:
            tally[primary] += 1

    return [(name.removeprefix("sig_"), tally.get(name, 0)) for name in SIGNAL_ORDER]


def risk_analytics(session: Session) -> RiskAnalytics:
    """Book-wide risk analytics: coverage (see book_coverage) plus the
    band/route/tier/band cuts, signal fire frequency, and primary-signal
    distribution, all read from client_risk_features -- the always-current
    cache, not a latest-run-only slice, same population book_coverage's
    scored_count counts against but every one of these cuts is over the
    whole current-state table.
    """
    coverage = book_coverage(session)

    def _counts(column) -> list[tuple]:
        return list(
            session.execute(
                select(column, func.count())
                .select_from(ClientRiskFeatures)
                .group_by(column)
                .order_by(func.count().desc())
            ).all()
        )

    signal_frequency = [
        (
            name.removeprefix("sig_"),
            session.scalar(
                select(func.count())
                .select_from(ClientRiskFeatures)
                .where(getattr(ClientRiskFeatures, name))
            )
            or 0,
        )
        for name in SIGNAL_ORDER
    ]

    total_aum_at_risk = (
        session.scalar(select(func.coalesce(func.sum(ClientRiskFeatures.aum_at_risk), 0.0))) or 0.0
    )

    return RiskAnalytics(
        book_size=coverage.book_size,
        scored_count=coverage.scored_count,
        as_of=coverage.as_of,
        by_risk_band=_counts(ClientRiskFeatures.risk_band),
        by_route=_counts(ClientRiskFeatures.route),
        by_balance_tier=_counts(ClientRiskFeatures.balance_tier),
        by_value_tier=_counts(ClientRiskFeatures.value_tier),
        by_recency_band=_counts(ClientRiskFeatures.recency_band),
        signal_frequency=signal_frequency,
        primary_signal_distribution=_primary_signal_distribution(session),
        total_aum_at_risk=total_aum_at_risk,
    )


@dataclass(frozen=True)
class RiskTrendPoint:
    """One completed nightly run's book-wide numbers: band composition,
    total AUM at risk, and average score, read from risk_snapshot -- the
    append-only history client_risk_features itself doesn't carry.
    """

    run_id: str
    as_of: datetime | None
    by_risk_band: list[tuple[str, int]]
    total_aum_at_risk: float
    avg_risk_score: float


def risk_trend(session: Session, *, runs: int = 30) -> list[RiskTrendPoint]:
    """The last `runs` completed nightly runs' book-wide numbers, oldest
    first, for trend charts. Only completed runs count, same rule
    book_coverage uses -- a run still "running" has no reliable snapshot
    population yet. Empty when no run has ever completed.
    """
    runs = max(1, min(runs, 90))

    recent_runs = list(
        session.scalars(
            select(RiskRun)
            .where(RiskRun.state == "completed")
            .order_by(RiskRun.finished_at.desc(), RiskRun.started_at.desc())
            .limit(runs)
        ).all()
    )
    recent_runs.reverse()

    points: list[RiskTrendPoint] = []
    for run in recent_runs:
        by_risk_band = list(
            session.execute(
                select(RiskSnapshot.risk_band, func.count())
                .where(RiskSnapshot.run_id == run.run_id)
                .group_by(RiskSnapshot.risk_band)
                .order_by(func.count().desc())
            ).all()
        )
        total_aum_at_risk = (
            session.scalar(
                select(func.coalesce(func.sum(RiskSnapshot.aum_at_risk), 0.0)).where(
                    RiskSnapshot.run_id == run.run_id
                )
            )
            or 0.0
        )
        avg_risk_score = (
            session.scalar(
                select(func.coalesce(func.avg(RiskSnapshot.risk_score), 0.0)).where(
                    RiskSnapshot.run_id == run.run_id
                )
            )
            or 0.0
        )
        points.append(
            RiskTrendPoint(
                run_id=run.run_id,
                as_of=run.finished_at,
                by_risk_band=by_risk_band,
                total_aum_at_risk=total_aum_at_risk,
                avg_risk_score=float(avg_risk_score),
            )
        )
    return points
