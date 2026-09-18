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

from app.agents.situations import (
    CONTRIBUTION_DECLINE,
    FOLLOW_UP_OVERDUE,
    HEALTHY_BANDS,
    NEW_CLIENT_SINGLE_DEPOSIT,
    RISK_ACTION_GAP,
    SINGLE_FUND_HEALTHY,
    SMALL_BALANCE_INACTIVE,
)
from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.digest import DigestLine, DigestRun
from app.db.models.risk import ClientRiskFeatures
from app.db.models.signals import ClientSituationState
from app.risk.history import latest_completed_run_id, routes_before_run, routes_for_run
from app.risk.routing import route_direction
from app.risk.store import load_active_config

SIGNED_UP_RECENTLY = "signed_up_recently"
FEE_PRESSURE_GONE_QUIET = "fee_pressure_gone_quiet"
FEE_PRESSURE_ACTIVE_CONTRIBUTOR = "fee_pressure_active_contributor"
VERY_SMALL_AND_QUIET = "very_small_and_quiet"
GETTING_SMALLER = "getting_smaller"
HEALTHY_ONE_FUND = "healthy_one_fund"
WAITING_ON_A_CALL = "waiting_on_a_call"
MORE_URGENT_BUT_NOT_CALLED = "more_urgent_but_not_called"

GROUP_NAMES = (
    SIGNED_UP_RECENTLY,
    FEE_PRESSURE_GONE_QUIET,
    FEE_PRESSURE_ACTIVE_CONTRIBUTOR,
    VERY_SMALL_AND_QUIET,
    GETTING_SMALLER,
    HEALTHY_ONE_FUND,
    WAITING_ON_A_CALL,
    MORE_URGENT_BUT_NOT_CALLED,
)

GROUP_TO_SITUATION: dict[str, str] = {
    SIGNED_UP_RECENTLY: NEW_CLIENT_SINGLE_DEPOSIT,
    FEE_PRESSURE_GONE_QUIET: FEE_PRESSURE_GONE_QUIET,
    FEE_PRESSURE_ACTIVE_CONTRIBUTOR: FEE_PRESSURE_ACTIVE_CONTRIBUTOR,
    VERY_SMALL_AND_QUIET: SMALL_BALANCE_INACTIVE,
    GETTING_SMALLER: CONTRIBUTION_DECLINE,
    HEALTHY_ONE_FUND: SINGLE_FUND_HEALTHY,
    WAITING_ON_A_CALL: FOLLOW_UP_OVERDUE,
    MORE_URGENT_BUT_NOT_CALLED: RISK_ACTION_GAP,
}

CALL_LIST_ROUTE = "fa_call_priority"
MORE_URGENT = "more_urgent"

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


def _signed_up_recently_legacy(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    earliest = as_of - timedelta(days=thresholds.new_client_days)
    statement = _client_funds().where(
        ActiveClientFund.n_deposits == 1,
        ActiveClientFund.first_deposit_date.is_not(None),
        ActiveClientFund.first_deposit_date >= earliest,
    )
    definition = {
        "situation_code": NEW_CLIENT_SINGLE_DEPOSIT,
        "is_active": True,
    }
    return _build_group(session, SIGNED_UP_RECENTLY, definition, statement)


def _signed_up_recently_from_situations(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    statement = (
        _client_funds()
        .join(
            ClientSituationState,
            (ClientSituationState.client_id == ActiveClientFund.client_id)
            & (ClientSituationState.unit_fund_id == ActiveClientFund.unit_fund_id),
        )
        .where(
            ClientSituationState.situation_code == NEW_CLIENT_SINGLE_DEPOSIT,
            ClientSituationState.is_active.is_(True),
        )
    )
    definition = {
        "situation_code": NEW_CLIENT_SINGLE_DEPOSIT,
        "is_active": True,
    }
    return _build_group(session, SIGNED_UP_RECENTLY, definition, statement)


def signed_up_recently(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    """Clients whose new_client_single_deposit situation is active."""
    if get_settings().signal_situation_source == "situations":
        return _signed_up_recently_from_situations(session, thresholds, as_of)
    return _signed_up_recently_legacy(session, thresholds, as_of)


def _situation_statement(situation_code: str) -> Select:
    return (
        _client_funds()
        .join(
            ClientSituationState,
            (ClientSituationState.client_id == ActiveClientFund.client_id)
            & (ClientSituationState.unit_fund_id == ActiveClientFund.unit_fund_id),
        )
        .where(
            ClientSituationState.situation_code == situation_code,
            ClientSituationState.is_active.is_(True),
        )
    )


def fee_pressure_gone_quiet(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    """Clients under fee pressure whose deposits have gone quiet."""
    definition = {"situation_code": FEE_PRESSURE_GONE_QUIET, "is_active": True}
    return _build_group(
        session,
        FEE_PRESSURE_GONE_QUIET,
        definition,
        _situation_statement(FEE_PRESSURE_GONE_QUIET),
    )


def fee_pressure_active_contributor(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    """Clients under fee pressure who are still paying in."""
    definition = {"situation_code": FEE_PRESSURE_ACTIVE_CONTRIBUTOR, "is_active": True}
    return _build_group(
        session,
        FEE_PRESSURE_ACTIVE_CONTRIBUTOR,
        definition,
        _situation_statement(FEE_PRESSURE_ACTIVE_CONTRIBUTOR),
    )


def _very_small_and_quiet_legacy(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
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


def _very_small_and_quiet_from_situations(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    definition = {"situation_code": SMALL_BALANCE_INACTIVE, "is_active": True}
    return _build_group(
        session, VERY_SMALL_AND_QUIET, definition, _situation_statement(SMALL_BALANCE_INACTIVE)
    )


def very_small_and_quiet(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    """Very small balances that have also stopped moving."""
    if get_settings().signal_situation_source == "situations":
        return _very_small_and_quiet_from_situations(session, thresholds, as_of)
    return _very_small_and_quiet_legacy(session, thresholds, as_of)


def _getting_smaller_legacy(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    statement = _risk_features_join(_client_funds()).where(
        ClientRiskFeatures.sig_shrinking.is_(True)
    )
    return _build_group(session, GETTING_SMALLER, {"sig_shrinking": True}, statement)


def _getting_smaller_from_situations(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    definition = {"situation_code": CONTRIBUTION_DECLINE, "is_active": True}
    return _build_group(
        session, GETTING_SMALLER, definition, _situation_statement(CONTRIBUTION_DECLINE)
    )


def getting_smaller(session: Session, thresholds: WatchlistThresholds, as_of: date) -> WatchGroup:
    """Clients whose payments in are going down over time."""
    if get_settings().signal_situation_source == "situations":
        return _getting_smaller_from_situations(session, thresholds, as_of)
    return _getting_smaller_legacy(session, thresholds, as_of)


def _healthy_one_fund_legacy(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
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


def _healthy_one_fund_from_situations(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    definition = {"situation_code": SINGLE_FUND_HEALTHY, "is_active": True}
    return _build_group(
        session, HEALTHY_ONE_FUND, definition, _situation_statement(SINGLE_FUND_HEALTHY)
    )


def healthy_one_fund(session: Session, thresholds: WatchlistThresholds, as_of: date) -> WatchGroup:
    """Clients in good shape who hold only one fund."""
    if get_settings().signal_situation_source == "situations":
        return _healthy_one_fund_from_situations(session, thresholds, as_of)
    return _healthy_one_fund_legacy(session, thresholds, as_of)


def _waiting_on_a_call_legacy(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
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


def _waiting_on_a_call_from_situations(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    definition = {"situation_code": FOLLOW_UP_OVERDUE, "is_active": True}
    return _build_group(
        session, WAITING_ON_A_CALL, definition, _situation_statement(FOLLOW_UP_OVERDUE)
    )


def waiting_on_a_call(session: Session, thresholds: WatchlistThresholds, as_of: date) -> WatchGroup:
    """Clients put on a call list a while back that nobody has touched since."""
    if get_settings().signal_situation_source == "situations":
        return _waiting_on_a_call_from_situations(session, thresholds, as_of)
    return _waiting_on_a_call_legacy(session, thresholds, as_of)


def _more_urgent_but_not_called_legacy(
    session: Session,
    thresholds: WatchlistThresholds,
    as_of: date,
    run_id: str | None = None,
) -> WatchGroup:
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


def _more_urgent_but_not_called_from_situations(
    session: Session, thresholds: WatchlistThresholds, as_of: date
) -> WatchGroup:
    definition = {"situation_code": RISK_ACTION_GAP, "is_active": True}
    return _build_group(
        session, MORE_URGENT_BUT_NOT_CALLED, definition, _situation_statement(RISK_ACTION_GAP)
    )


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
    if get_settings().signal_situation_source == "situations":
        return _more_urgent_but_not_called_from_situations(session, thresholds, as_of)
    return _more_urgent_but_not_called_legacy(session, thresholds, as_of, run_id)


GROUP_FILTERS = (
    signed_up_recently,
    fee_pressure_gone_quiet,
    fee_pressure_active_contributor,
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
