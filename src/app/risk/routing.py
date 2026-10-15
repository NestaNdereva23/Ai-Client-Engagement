"""Route names and how urgent each one is.

Every active client is on fa_action. The five older queue names stay so stored
history and audit entries still read.
"""

from __future__ import annotations

from dataclasses import dataclass

ROUTE_FA_ACTION = "fa_action"

ROUTES = (
    "small_balance_review",
    "fa_call_priority",
    "fa_watchlist",
    "auto_checkin",
    "monitor_only",
    ROUTE_FA_ACTION,
)

# How urgent each route is for a human to act on, lowest to highest. Used
# only to label a route move as an improvement or an escalation -- it does
# not affect routing itself. small_balance_review sits at the bottom:
# balance is too small to be worth an FA's time even though a signal fired,
# so leaving any other route for small_balance_review is always an
# improvement, never a regression.
ROUTE_SEVERITY: dict[str, int | None] = {
    "small_balance_review": 0,
    "monitor_only": 1,
    "auto_checkin": 2,
    "fa_watchlist": 3,
    "fa_call_priority": 4,
    ROUTE_FA_ACTION: None,  # no urgency of its own, so moves into or out of it are never labelled
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


def _severity(route: str) -> int | None:
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
    if before is None or after is None:
        return None
    if after > before:
        return "more_urgent"
    if after < before:
        return "less_urgent"
    return "unchanged"


@dataclass
class RouteResult:
    route: str
    queue_rank: int | None
    # Carried alongside the route, not stored on client_risk_features --
    # the digest and briefing (AM10, AM11) read this straight from here
    # rather than re-querying complaints for every row they render.
    complaint_caveat: bool


def route_for_active_client(has_open_complaint: bool) -> RouteResult:
    return RouteResult(route=ROUTE_FA_ACTION, queue_rank=None, complaint_caveat=has_open_complaint)
