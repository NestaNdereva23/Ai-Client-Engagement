"""Score history: writing risk_snapshot rows and reading the delta between
two consecutive ones.

Append-only. write_snapshot never upserts -- a repeat write for the same
run_id/client_id/unit_fund_id hits risk_snapshot's own unique constraint
and raises, rather than silently overwriting a night that already ran.

"Most recent" is read off snapshot_id, not run_id or a timestamp: run_id is
a random uuid with no ordering of its own, and two runs can start within the
same clock tick. snapshot_id is a plain autoincrement, so it grows in
exactly the order rows were written -- which is exactly the order runs
happened, since every run writes its snapshots before the next run starts.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from app.db.models.risk import RiskRun, RiskSnapshot
from app.risk.routing import RouteResult
from app.risk.scoring import ScoreResult


def write_snapshot(
    session: Session,
    run_id: str,
    client_id: int,
    unit_fund_id: int,
    score: ScoreResult,
    route: RouteResult,
    config_version: int,
    *,
    pattern_is_reliable: bool,
    overdue_multiple: float | None,
) -> RiskSnapshot:
    """Append one client-fund's numbers for one run."""
    row = RiskSnapshot(
        run_id=run_id,
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        recency_band=score.recency_band,
        balance_tier=score.balance_tier,
        value_tier=score.value_tier,
        pattern_is_reliable=pattern_is_reliable,
        overdue_multiple=overdue_multiple,
        risk_score=score.risk_score,
        risk_band=score.risk_band,
        risk_reasons=score.risk_reasons,
        fund_at_risk=score.fund_at_risk,
        config_version=config_version,
        route=route.route,
        queue_rank=route.queue_rank,
        **score.signals,
    )
    session.add(row)
    session.flush()
    return row


def latest_snapshot_for(
    session: Session, client_id: int, unit_fund_id: int, before_snapshot_id: int | None = None
) -> RiskSnapshot | None:
    """The most recently written snapshot for one client-fund.

    before_snapshot_id excludes that row and everything written after it, so
    delta_for can ask "what was true immediately before this one".
    """
    query = select(RiskSnapshot).where(
        RiskSnapshot.client_id == client_id, RiskSnapshot.unit_fund_id == unit_fund_id
    )
    if before_snapshot_id is not None:
        query = query.where(RiskSnapshot.snapshot_id < before_snapshot_id)
    query = query.order_by(RiskSnapshot.snapshot_id.desc()).limit(1)
    return session.scalar(query)


def delta_for(session: Session, client_id: int, unit_fund_id: int, run_id: str) -> int | None:
    """today's risk_score minus the most recent prior snapshot's, for one
    client-fund.

    None on a client's first-ever run -- never a fabricated zero, since "no
    change" and "no history yet" are different facts and a digest needs to
    tell them apart.
    """
    current = session.scalar(
        select(RiskSnapshot).where(
            RiskSnapshot.run_id == run_id,
            RiskSnapshot.client_id == client_id,
            RiskSnapshot.unit_fund_id == unit_fund_id,
        )
    )
    if current is None:
        return None
    previous = latest_snapshot_for(
        session, client_id, unit_fund_id, before_snapshot_id=current.snapshot_id
    )
    if previous is None:
        return None
    return current.risk_score - previous.risk_score


def previous_scores(
    session: Session, run_id: str, keys: Sequence[tuple[int, int]]
) -> dict[tuple[int, int], int]:
    """Each key's risk_score from the most recent run before this one.

    One read for the whole population, so a caller counting how many clients
    got worse overnight does not pay two queries per client the way
    delta_for does. A key with no earlier snapshot is absent from the
    result, which is the same "no history yet" delta_for reports as None.
    """
    keys = list(keys)
    if not keys:
        return {}
    latest = (
        select(
            RiskSnapshot.client_id,
            RiskSnapshot.unit_fund_id,
            RiskSnapshot.risk_score,
        )
        .where(
            RiskSnapshot.run_id != run_id,
            tuple_(RiskSnapshot.client_id, RiskSnapshot.unit_fund_id).in_(keys),
        )
        .distinct(RiskSnapshot.client_id, RiskSnapshot.unit_fund_id)
        .order_by(
            RiskSnapshot.client_id,
            RiskSnapshot.unit_fund_id,
            RiskSnapshot.snapshot_id.desc(),
        )
    )
    return {(row.client_id, row.unit_fund_id): row.risk_score for row in session.execute(latest)}


def latest_completed_run_id(session: Session) -> str | None:
    """The most recent nightly run that finished, or None if none has.

    A run still going never counts, so a caller never compares tonight's
    half written routes against last night's finished ones.
    """
    return session.scalar(
        select(RiskRun.run_id)
        .where(RiskRun.state == "completed")
        .order_by(RiskRun.finished_at.desc(), RiskRun.started_at.desc())
        .limit(1)
    )


def routes_for_run(session: Session, run_id: str) -> dict[tuple[int, int], str | None]:
    """Every client fund's queue as this run left it, in one read."""
    rows = session.execute(
        select(RiskSnapshot.client_id, RiskSnapshot.unit_fund_id, RiskSnapshot.route).where(
            RiskSnapshot.run_id == run_id
        )
    )
    return {(row.client_id, row.unit_fund_id): row.route for row in rows}


def routes_before_run(session: Session, run_id: str) -> dict[tuple[int, int], str | None]:
    """Every client fund's queue as the run before this one left it.

    Bounded by the first snapshot this run wrote rather than by run_id
    alone, so a run that started later can never be mistaken for the one
    that came before. A client fund with no earlier snapshot is absent, the
    same "no history yet" previous_scores reports.
    """
    first_snapshot = (
        select(func.min(RiskSnapshot.snapshot_id))
        .where(RiskSnapshot.run_id == run_id)
        .scalar_subquery()
    )
    rows = session.execute(
        select(RiskSnapshot.client_id, RiskSnapshot.unit_fund_id, RiskSnapshot.route)
        .where(RiskSnapshot.snapshot_id < first_snapshot)
        .distinct(RiskSnapshot.client_id, RiskSnapshot.unit_fund_id)
        .order_by(
            RiskSnapshot.client_id,
            RiskSnapshot.unit_fund_id,
            RiskSnapshot.snapshot_id.desc(),
        )
    )
    return {(row.client_id, row.unit_fund_id): row.route for row in rows}
