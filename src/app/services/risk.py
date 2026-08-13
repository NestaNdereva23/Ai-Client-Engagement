"""Read-only views over the current risk state.

Everything here reads client_risk_features, the always-current cache of the
latest nightly run's numbers (see db/models/risk.py's own docstring) --
never risk_snapshot, since these are "what does ops need to act on right
now" reads, not a history read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from app.db.models.active_clients import ActiveClientFund
from app.db.models.risk import ClientRiskFeatures, RiskRun, RiskSnapshot
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_pair_cursor, encode_pair_cursor

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
