"""The five-queue router: small_balance_review, fa_call_priority,
fa_watchlist, auto_checkin, monitor_only.

Evaluated top to bottom, first match wins. Pure over already-gathered
inputs -- no DB access here, same discipline as risk/scoring.py. A caller
(the nightly detection job) fetches complaints and suppression, gathers one
RoutableRow per client-fund, and calls route_population once for the whole
run so the capacity-bounded call queue can be computed across everyone at
once.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models.risk import RiskConfigVersion

ROUTES = (
    "small_balance_review",
    "fa_call_priority",
    "fa_watchlist",
    "auto_checkin",
    "monitor_only",
)

# How urgent each route is for a human to act on, lowest to highest. Used
# only to label a route move as an improvement or an escalation -- it does
# not affect routing itself. small_balance_review sits at the bottom:
# balance is too small to be worth an FA's time even though a signal fired,
# so leaving any other route for small_balance_review is always an
# improvement, never a regression.
ROUTE_SEVERITY = {
    "small_balance_review": 0,
    "monitor_only": 1,
    "auto_checkin": 2,
    "fa_watchlist": 3,
    "fa_call_priority": 4,
}

# Route names as spelled before the terminology rename (see migration
# ece296116d34). That migration deliberately leaves already-written
# risk_snapshot/audit_log history in its original wording rather than
# rewriting it, so a from_route/to_route pulled out of an old nightly run's
# audit trail can still arrive here under the retired name. Only used to
# resolve a severity rank -- the retired spelling is still what callers
# display for history, this does not touch that.
_RETIRED_ROUTE_NAMES = {
    "dust_cleanup": "small_balance_review",
    "fa_digest_watch": "fa_watchlist",
    "automated_nurture": "auto_checkin",
}


def _severity(route: str) -> int:
    return ROUTE_SEVERITY[_RETIRED_ROUTE_NAMES.get(route, route)]


def route_direction(from_route: str | None, to_route: str) -> str | None:
    """Whether a route move makes the client more or less urgent for a
    human to act on -- not whether the client's own situation got better or
    worse (a move to small_balance_review is "less_urgent" even though it
    usually means the client already withdrew almost everything).

    "more_urgent" when to_route outranks from_route, "less_urgent" when it
    ranks lower. None when there's no prior route to compare against (a
    client's first-ever scored run).
    """
    if from_route is None:
        return None
    before = _severity(from_route)
    after = _severity(to_route)
    if after > before:
        return "more_urgent"
    if after < before:
        return "less_urgent"
    return "unchanged"


@dataclass
class RoutableRow:
    """One client-fund's routing inputs.

    has_open_complaint and suppressed default false, since most runs will
    not have either for most clients -- callers only need to set them for
    the rows that actually carry one.
    """

    key: tuple[int, int]
    balance: float | None
    risk_score: float
    sig_dormant: bool
    fund_at_risk: float
    has_open_complaint: bool = False
    suppressed: bool = False


@dataclass
class RouteResult:
    route: str
    queue_rank: int | None
    # Carried alongside the route, not stored on client_risk_features --
    # the digest and briefing (AM10, AM11) read this straight from here
    # rather than re-querying complaints for every row they render.
    complaint_caveat: bool


def _is_tiny_balance(balance: float | None, tiny_balance: float) -> bool:
    return (balance or 0.0) < tiny_balance


def _is_worth_a_call(balance: float | None, worth_a_call_balance: float) -> bool:
    return (balance or 0.0) >= worth_a_call_balance


def _is_at_risk(risk_score: float, at_risk_min: int) -> bool:
    return risk_score >= at_risk_min


def call_queue_keys(rows: list[RoutableRow], config: RiskConfigVersion) -> set[tuple[int, int]]:
    """The top fa_call_capacity client-funds by fund_at_risk, from the
    worth-a-call-and-at-risk pool only.

    Capacity-bounded, not threshold-bounded: the queue is the top N the
    team can actually call, not everyone who clears a bar.
    """
    worth_a_call_balance = config.thresholds["WORTH_A_CALL_BALANCE"]
    pool = [
        row
        for row in rows
        if _is_worth_a_call(row.balance, worth_a_call_balance)
        and _is_at_risk(row.risk_score, config.at_risk_min)
    ]
    pool.sort(key=lambda row: row.fund_at_risk, reverse=True)
    return {row.key for row in pool[: config.fa_call_capacity]}


def _base_route(row: RoutableRow, config: RiskConfigVersion, in_call_queue: bool) -> str:
    """The five queues on signals alone, no complaint or suppression override."""
    thresholds = config.thresholds
    tiny_balance = _is_tiny_balance(row.balance, thresholds["TINY_BALANCE"])
    worth_a_call = _is_worth_a_call(row.balance, thresholds["WORTH_A_CALL_BALANCE"])
    at_risk = _is_at_risk(row.risk_score, config.at_risk_min)

    if tiny_balance and row.sig_dormant:
        return "small_balance_review"
    if in_call_queue:
        return "fa_call_priority"
    if worth_a_call and at_risk:
        return "fa_watchlist"
    if not tiny_balance and at_risk:
        return "auto_checkin"
    return "monitor_only"


def route_population(
    rows: list[RoutableRow], config: RiskConfigVersion
) -> dict[tuple[int, int], RouteResult]:
    """Route every row in one run, capacity and overrides included."""
    queue = call_queue_keys(rows, config)
    ranked = sorted(
        (row for row in rows if row.key in queue), key=lambda row: row.fund_at_risk, reverse=True
    )
    queue_rank = {row.key: rank for rank, row in enumerate(ranked, start=1)}

    results: dict[tuple[int, int], RouteResult] = {}
    for row in rows:
        in_call_queue = row.key in queue
        final = _base_route(row, config, in_call_queue)

        # Complaint override: auto_checkin never reaches a client with an
        # open complaint. They get whichever of fa_watchlist or
        # fa_call_priority their balance and score already earn -- in
        # practice fa_watchlist, since auto_checkin only ever reaches
        # clients below the worth-a-call balance and the call queue is
        # worth-a-call only.
        if final == "auto_checkin" and row.has_open_complaint:
            final = "fa_call_priority" if in_call_queue else "fa_watchlist"
        # Suppression: auto_checkin never reaches a suppressed client, the
        # same gate Phase 1's eligibility check applies before every touch.
        # Unlike a complaint, suppression earns no escalation -- it falls
        # through to monitor_only, since it means "do not contact", not
        # "needs a human instead".
        elif final == "auto_checkin" and row.suppressed:
            final = "monitor_only"

        results[row.key] = RouteResult(
            route=final,
            queue_rank=queue_rank.get(row.key) if final == "fa_call_priority" else None,
            complaint_caveat=row.has_open_complaint,
        )
    return results
