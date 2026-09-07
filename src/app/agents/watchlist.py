"""Tonight's groups, found by plain database queries over the freshly
scored book.

A group is a fact, not a judgement: every function here is a filter with a
name. Choosing what to do with a group happens later, somewhere else.

Groups carry client and fund ids, counts and money totals. They never carry
a client name.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.digest import DigestLine, DigestRun
from app.db.models.risk import ClientRiskFeatures
from app.risk.history import latest_completed_run_id, routes_before_run, routes_for_run
from app.risk.routing import route_direction
from app.risk.store import load_active_config

SIGNED_UP_RECENTLY = "signed_up_recently"
FEES_WILL_EMPTY = "fees_will_empty"
VERY_SMALL_AND_QUIET = "very_small_and_quiet"
GETTING_SMALLER = "getting_smaller"
HEALTHY_ONE_FUND = "healthy_one_fund"
WAITING_ON_A_CALL = "waiting_on_a_call"
MORE_URGENT_BUT_NOT_CALLED = "more_urgent_but_not_called"

GROUP_NAMES = (
    SIGNED_UP_RECENTLY,
    FEES_WILL_EMPTY,
    VERY_SMALL_AND_QUIET,
    GETTING_SMALLER,
    HEALTHY_ONE_FUND,
    WAITING_ON_A_CALL,
    MORE_URGENT_BUT_NOT_CALLED,
)

HEALTHY_BANDS = ("None", "Low")

CALL_LIST_ROUTE = "fa_call_priority"
MORE_URGENT = "more_urgent"

ONE_DEPOSIT = 1
ONE_FUND = 1


class WatchlistConfigMissing(LookupError):
    """No risk config version is in force, so the thresholds cannot be read."""


@dataclass(frozen=True)
class WatchlistThresholds:
    """Every number the group filters need, read from settings and from the
    risk config version in force.
    """

    new_client_days: int
    months_until_empty: float
    small_balance: float
    awaiting_call_days: int


@dataclass(frozen=True)
class GroupMember:
    """One client fund in a group. Pseudonymous keys and a balance only."""

    client_id: int
    unit_fund_id: int
    balance: float


@dataclass(frozen=True)
class WatchGroup:
    """One named group: who is in it, how many clients, and how much money."""

    name: str
    definition: dict = field(default_factory=dict)
    members: tuple[GroupMember, ...] = ()

    @property
    def client_count(self) -> int:
        """How many separate clients the group holds."""
        return len({member.client_id for member in self.members})

    @property
    def fund_count(self) -> int:
        """How many client funds the group holds."""
        return len(self.members)

    @property
    def money_total(self) -> float:
        """The balances of every client fund in the group, added up."""
        return sum(member.balance for member in self.members)


def load_thresholds(session: Session, as_of: date) -> WatchlistThresholds:
    """Read the thresholds in force on the given day.

    Two of them are agent settings. The other two come from the risk config
    version the nightly run scored against, so a group is always cut at the
    same numbers that produced the scores.
    """
    config = load_active_config(session, as_of)
    if config is None:
        raise WatchlistConfigMissing(f"no risk config version is in force on {as_of}")
    settings = get_settings()
    return WatchlistThresholds(
        new_client_days=settings.agent_new_client_days,
        months_until_empty=float(config.thresholds["MONTHS_UNTIL_EMPTY"]),
        small_balance=float(config.thresholds["TINY_BALANCE"]),
        awaiting_call_days=settings.agent_awaiting_call_days,
    )


def _client_funds() -> Select:
    return select(
        ActiveClientFund.client_id,
        ActiveClientFund.unit_fund_id,
        ActiveClientFund.balance,
    )


def _risk_features_join(statement: Select) -> Select:
    return statement.join(
        ClientRiskFeatures,
        (ClientRiskFeatures.client_id == ActiveClientFund.client_id)
        & (ClientRiskFeatures.unit_fund_id == ActiveClientFund.unit_fund_id),
    )


def _build_group(session: Session, name: str, definition: dict, statement: Select) -> WatchGroup:
    members = tuple(
        GroupMember(
            client_id=row.client_id,
            unit_fund_id=row.unit_fund_id,
            balance=float(row.balance or 0.0),
        )
        for row in session.execute(statement).all()
    )
    return WatchGroup(name=name, definition=definition, members=members)


def signed_up_recently(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    """Clients who paid in once and did so within the last few days."""
    earliest = as_of - timedelta(days=thresholds.new_client_days)
    statement = _client_funds().where(
        ActiveClientFund.n_deposits == ONE_DEPOSIT,
        ActiveClientFund.first_deposit_date.is_not(None),
        ActiveClientFund.first_deposit_date >= earliest,
    )
    definition = {
        "deposits_made": ONE_DEPOSIT,
        "first_deposit_on_or_after": earliest.isoformat(),
    }
    return _build_group(session, SIGNED_UP_RECENTLY, definition, statement)


def fees_will_empty(session: Session, thresholds: WatchlistThresholds, as_of: date) -> WatchGroup:
    """Accounts with money left that the monthly fee will run down soon."""
    statement = _client_funds().where(
        ActiveClientFund.months_until_empty.is_not(None),
        ActiveClientFund.months_until_empty < thresholds.months_until_empty,
        ActiveClientFund.balance > 0,
    )
    definition = {
        "months_until_empty_below": thresholds.months_until_empty,
        "balance_above": 0,
    }
    return _build_group(session, FEES_WILL_EMPTY, definition, statement)


def very_small_and_quiet(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    """Very small balances that have also stopped moving."""
    statement = _risk_features_join(_client_funds()).where(
        ActiveClientFund.balance.is_not(None),
        ActiveClientFund.balance < thresholds.small_balance,
        ClientRiskFeatures.sig_dormant.is_(True),
    )
    definition = {
        "balance_below": thresholds.small_balance,
        "sig_dormant": True,
    }
    return _build_group(session, VERY_SMALL_AND_QUIET, definition, statement)


def getting_smaller(session: Session, thresholds: WatchlistThresholds, as_of: date) -> WatchGroup:
    """Clients whose payments in are going down over time."""
    statement = _risk_features_join(_client_funds()).where(
        ClientRiskFeatures.sig_shrinking.is_(True)
    )
    return _build_group(session, GETTING_SMALLER, {"sig_shrinking": True}, statement)


def healthy_one_fund(session: Session, thresholds: WatchlistThresholds, as_of: date) -> WatchGroup:
    """Clients in good shape who hold only one fund."""
    funds_held = (
        select(ActiveClientFund.client_id, func.count().label("funds_held"))
        .group_by(ActiveClientFund.client_id)
        .subquery()
    )
    statement = (
        _risk_features_join(_client_funds())
        .join(funds_held, funds_held.c.client_id == ActiveClientFund.client_id)
        .where(
            ClientRiskFeatures.risk_band.in_(HEALTHY_BANDS),
            funds_held.c.funds_held == ONE_FUND,
        )
    )
    definition = {"risk_band_in": list(HEALTHY_BANDS), "funds_held": ONE_FUND}
    return _build_group(session, HEALTHY_ONE_FUND, definition, statement)


def waiting_on_a_call(session: Session, thresholds: WatchlistThresholds, as_of: date) -> WatchGroup:
    """Clients put on a call list a while back that nobody has touched since."""
    cutoff = as_of - timedelta(days=thresholds.awaiting_call_days)
    something_logged_since = (
        select(ActiveClientInteraction.id)
        .where(
            ActiveClientInteraction.client_id == DigestLine.client_id,
            ActiveClientInteraction.unit_fund_id == DigestLine.unit_fund_id,
            ActiveClientInteraction.created_at >= DigestRun.generated_at,
        )
        .exists()
    )
    statement = (
        _client_funds()
        .join(
            DigestLine,
            (DigestLine.client_id == ActiveClientFund.client_id)
            & (DigestLine.unit_fund_id == ActiveClientFund.unit_fund_id),
        )
        .join(DigestRun, DigestRun.digest_run_id == DigestLine.digest_run_id)
        .where(
            DigestLine.in_call_queue.is_(True),
            DigestRun.generated_at < cutoff,
            ~something_logged_since,
        )
        .distinct()
    )
    definition = {
        "in_call_queue": True,
        "digest_generated_before": cutoff.isoformat(),
        "nothing_logged_since_the_digest": True,
    }
    return _build_group(session, WAITING_ON_A_CALL, definition, statement)


def more_urgent_but_not_called(
    session: Session,
    thresholds: WatchlistThresholds,
    as_of: date,
    run_id: str | None = None,
) -> WatchGroup:
    """Clients the last run moved to a more urgent queue who still did not
    make that morning's call list.

    These are the ones a person would otherwise never hear about: their
    situation got worse overnight, but there was no room for them on the
    call list, so nobody rings them and nothing else picks them up.
    """
    definition = {"queue_moved": MORE_URGENT, "on_the_call_list": False}
    if run_id is None:
        run_id = latest_completed_run_id(session)
    if run_id is None:
        return WatchGroup(name=MORE_URGENT_BUT_NOT_CALLED, definition=definition)

    current = routes_for_run(session, run_id)
    previous = routes_before_run(session, run_id)
    keys = [
        key
        for key, route in current.items()
        if route is not None
        and route != CALL_LIST_ROUTE
        and route_direction(previous.get(key), route) == MORE_URGENT
    ]
    if not keys:
        return WatchGroup(name=MORE_URGENT_BUT_NOT_CALLED, definition=definition)

    statement = _client_funds().where(
        tuple_(ActiveClientFund.client_id, ActiveClientFund.unit_fund_id).in_(keys)
    )
    return _build_group(session, MORE_URGENT_BUT_NOT_CALLED, definition, statement)


GROUP_FILTERS = (
    signed_up_recently,
    fees_will_empty,
    very_small_and_quiet,
    getting_smaller,
    healthy_one_fund,
    waiting_on_a_call,
    more_urgent_but_not_called,
)


def build_watchlist(
    session: Session,
    as_of: date,
    thresholds: WatchlistThresholds | None = None,
) -> Sequence[WatchGroup]:
    """Run every group filter and return each group, empty ones included."""
    if thresholds is None:
        thresholds = load_thresholds(session, as_of)
    return tuple(build_group(session, thresholds, as_of) for build_group in GROUP_FILTERS)
