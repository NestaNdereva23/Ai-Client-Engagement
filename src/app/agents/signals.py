from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.signal_config import active_threshold
from app.db.bulk import copy_insert, copy_upsert
from app.db.models.active_clients import (
    ActiveClientFund,
    ActiveClientInteraction,
    ActiveTransaction,
)
from app.db.models.digest import DigestLine, DigestRun
from app.db.models.routing import ClientSide, ClientTier
from app.db.models.signals import ClientSignalSnapshot, ClientSignalState, SignalRun
from app.risk.history import latest_completed_run_id, routes_before_run, routes_for_run
from app.risk.routing import route_direction
from app.risk.store import load_active_config
from app.routing.sides import SIDE_ACTIVE, SIDE_INACTIVE
from app.routing.tiers import (
    SEGMENT_ACTIVE_ACTIVE,
    SEGMENT_ACTIVE_DORMANT,
    SEGMENT_BY_TIER,
    load_tier_thresholds_in_force,
)

SINGLE_DEPOSIT = "single_deposit"
FIRST_DEPOSIT_RECENT = "first_deposit_recent"
WINDOW_DAYS = "window_days"
HORIZON_MONTHS = "horizon_months"

FEE_PRESSURE_CLOSE = "fee_pressure_close"
SMALL_BALANCE = "small_balance"
SINGLE_FUND_HELD = "single_fund_held"
CALL_FOLLOW_UP_OVERDUE = "call_follow_up_overdue"
ROUTE_ESCALATED_THIS_RUN = "route_escalated_this_run"
NO_RECENT_PURCHASE = "no_recent_purchase"
PURCHASED_THIS_PERIOD = "purchased_this_period"
REAL_WITHDRAWER = "real_withdrawer"
PAST_TYPICAL_GAP = "past_typical_gap"
NET_FLOW_NEGATIVE = "net_flow_negative"
NEAR_PRESERVATION_LINE = "near_preservation_line"
SIDE_CHANGED_DOWNWARD = "side_changed_downward"
SEGMENT_MOVED_UP = "segment_moved_up"

SIGNAL_CODES = (
    SINGLE_DEPOSIT,
    FIRST_DEPOSIT_RECENT,
    FEE_PRESSURE_CLOSE,
    SMALL_BALANCE,
    SINGLE_FUND_HELD,
    CALL_FOLLOW_UP_OVERDUE,
    ROUTE_ESCALATED_THIS_RUN,
    NO_RECENT_PURCHASE,
    PURCHASED_THIS_PERIOD,
    REAL_WITHDRAWER,
    PAST_TYPICAL_GAP,
    NET_FLOW_NEGATIVE,
    NEAR_PRESERVATION_LINE,
    SIDE_CHANGED_DOWNWARD,
    SEGMENT_MOVED_UP,
)

ONE_DEPOSIT = 1
ONE_FUND = 1

PURCHASE_TXN = "purchase"
SALE_TXN = "sale"
MONTHS_PER_YEAR = 12
DAYS_PER_YEAR = 365

CALL_LIST_ROUTE = "fa_call_priority"
MORE_URGENT = "more_urgent"


class NewClientWindowMissing(LookupError):
    pass


class SignalThresholdMissing(LookupError):
    pass


class RiskConfigMissing(LookupError):
    pass


def _signal_values(
    n_deposits: int, first_deposit_date: date | None, as_of: date, window_days: int
) -> dict[str, bool]:
    earliest = as_of - timedelta(days=window_days)
    return {
        SINGLE_DEPOSIT: n_deposits == ONE_DEPOSIT,
        FIRST_DEPOSIT_RECENT: first_deposit_date is not None and first_deposit_date >= earliest,
    }


# since only moves when a signal flips; an unchanged signal keeps its original date.
_STATE_SET_SQL = (
    "is_active = EXCLUDED.is_active, "
    "run_id = EXCLUDED.run_id, "
    "updated_at = now(), "
    "since = CASE WHEN client_signal_state.is_active <> EXCLUDED.is_active "
    "THEN EXCLUDED.since ELSE client_signal_state.since END"
)


class _SignalWrites:
    """Collects one run's signal rows so they can be written in bulk."""

    def __init__(self, run_id: str, as_of: date) -> None:
        self.run_id = run_id
        self.as_of = as_of
        self.snapshots: list[dict] = []
        self.states: list[dict] = []

    def add(self, client_id: int, unit_fund_id: int, signal_code: str, is_active: bool) -> None:
        self.snapshots.append(
            {
                "run_id": self.run_id,
                "client_id": client_id,
                "unit_fund_id": unit_fund_id,
                "signal_code": signal_code,
                "is_active": is_active,
            }
        )
        self.states.append(
            {
                "client_id": client_id,
                "unit_fund_id": unit_fund_id,
                "signal_code": signal_code,
                "is_active": is_active,
                "since": self.as_of,
                "run_id": self.run_id,
            }
        )

    def flush(self, session: Session) -> None:
        copy_insert(session, ClientSignalSnapshot.__tablename__, self.snapshots)
        copy_upsert(
            session,
            ClientSignalState.__tablename__,
            self.states,
            ["client_id", "unit_fund_id", "signal_code"],
            _STATE_SET_SQL,
        )


def _open_run(session: Session) -> SignalRun:
    run = SignalRun(run_id=uuid4().hex)
    session.add(run)
    session.flush()
    return run


def _close_run(session: Session, run: SignalRun) -> SignalRun:
    run.state = "completed"
    run.finished_at = func.now()
    session.commit()
    return run


def recompute_new_client_signals(
    session: Session, as_of: date, run: SignalRun | None = None
) -> SignalRun:
    run_to_use = run or _open_run(session)

    window_days = active_threshold(session, FIRST_DEPOSIT_RECENT, WINDOW_DAYS, as_of)
    if window_days is None:
        raise NewClientWindowMissing(
            f"no {WINDOW_DAYS} threshold for {FIRST_DEPOSIT_RECENT} is in force on {as_of}"
        )

    funds = session.execute(
        select(
            ActiveClientFund.client_id,
            ActiveClientFund.unit_fund_id,
            ActiveClientFund.n_deposits,
            ActiveClientFund.first_deposit_date,
        )
    ).all()

    writes = _SignalWrites(run_to_use.run_id, as_of)
    for fund in funds:
        values = _signal_values(fund.n_deposits, fund.first_deposit_date, as_of, int(window_days))
        for signal_code, is_active in values.items():
            writes.add(fund.client_id, fund.unit_fund_id, signal_code, is_active)
    writes.flush(session)

    if run is None:
        return _close_run(session, run_to_use)
    return run_to_use


def recompute_fee_pressure_signal(
    session: Session, as_of: date, months_until_empty_threshold: float, run: SignalRun | None = None
) -> SignalRun:
    run_to_use = run or _open_run(session)

    funds = session.execute(
        select(
            ActiveClientFund.client_id,
            ActiveClientFund.unit_fund_id,
            ActiveClientFund.months_until_empty,
            ActiveClientFund.balance,
        )
    ).all()

    writes = _SignalWrites(run_to_use.run_id, as_of)
    for fund in funds:
        is_active = (
            fund.months_until_empty is not None
            and fund.months_until_empty < months_until_empty_threshold
            and (fund.balance or 0) > 0
        )
        writes.add(fund.client_id, fund.unit_fund_id, FEE_PRESSURE_CLOSE, is_active)
    writes.flush(session)

    if run is None:
        return _close_run(session, run_to_use)
    return run_to_use


def recompute_small_balance_signal(
    session: Session, as_of: date, small_balance_threshold: float, run: SignalRun | None = None
) -> SignalRun:
    run_to_use = run or _open_run(session)

    funds = session.execute(
        select(ActiveClientFund.client_id, ActiveClientFund.unit_fund_id, ActiveClientFund.balance)
    ).all()

    writes = _SignalWrites(run_to_use.run_id, as_of)
    for fund in funds:
        is_active = fund.balance is not None and fund.balance < small_balance_threshold
        writes.add(fund.client_id, fund.unit_fund_id, SMALL_BALANCE, is_active)
    writes.flush(session)

    if run is None:
        return _close_run(session, run_to_use)
    return run_to_use


def recompute_single_fund_held_signal(
    session: Session, as_of: date, run: SignalRun | None = None
) -> SignalRun:
    run_to_use = run or _open_run(session)

    funds_held = (
        select(ActiveClientFund.client_id, func.count().label("funds_held"))
        .group_by(ActiveClientFund.client_id)
        .subquery()
    )
    funds = session.execute(
        select(
            ActiveClientFund.client_id,
            ActiveClientFund.unit_fund_id,
            funds_held.c.funds_held,
        ).join(funds_held, funds_held.c.client_id == ActiveClientFund.client_id)
    ).all()

    writes = _SignalWrites(run_to_use.run_id, as_of)
    for fund in funds:
        is_active = fund.funds_held == ONE_FUND
        writes.add(fund.client_id, fund.unit_fund_id, SINGLE_FUND_HELD, is_active)
    writes.flush(session)

    if run is None:
        return _close_run(session, run_to_use)
    return run_to_use


def recompute_call_follow_up_overdue_signal(
    session: Session, as_of: date, awaiting_call_days: int, run: SignalRun | None = None
) -> SignalRun:
    run_to_use = run or _open_run(session)

    cutoff = as_of - timedelta(days=awaiting_call_days)
    something_logged_since = (
        select(ActiveClientInteraction.id)
        .where(
            ActiveClientInteraction.client_id == DigestLine.client_id,
            ActiveClientInteraction.unit_fund_id == DigestLine.unit_fund_id,
            ActiveClientInteraction.created_at >= DigestRun.generated_at,
        )
        .exists()
    )
    overdue_rows = session.execute(
        select(ActiveClientFund.client_id, ActiveClientFund.unit_fund_id)
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
    ).all()
    overdue_keys = {(row.client_id, row.unit_fund_id) for row in overdue_rows}

    funds = session.execute(select(ActiveClientFund.client_id, ActiveClientFund.unit_fund_id)).all()

    writes = _SignalWrites(run_to_use.run_id, as_of)
    for fund in funds:
        is_active = (fund.client_id, fund.unit_fund_id) in overdue_keys
        writes.add(fund.client_id, fund.unit_fund_id, CALL_FOLLOW_UP_OVERDUE, is_active)
    writes.flush(session)

    if run is None:
        return _close_run(session, run_to_use)
    return run_to_use


def recompute_route_escalated_signal(
    session: Session, as_of: date, risk_run_id: str | None = None, run: SignalRun | None = None
) -> SignalRun:
    run_to_use = run or _open_run(session)

    if risk_run_id is None:
        risk_run_id = latest_completed_run_id(session)

    escalated_keys: set[tuple[int, int]] = set()
    if risk_run_id is not None:
        current = routes_for_run(session, risk_run_id)
        previous = routes_before_run(session, risk_run_id)
        escalated_keys = {
            key
            for key, route in current.items()
            if route is not None
            and route != CALL_LIST_ROUTE
            and route_direction(previous.get(key), route) == MORE_URGENT
        }

    funds = session.execute(select(ActiveClientFund.client_id, ActiveClientFund.unit_fund_id)).all()

    writes = _SignalWrites(run_to_use.run_id, as_of)
    for fund in funds:
        is_active = (fund.client_id, fund.unit_fund_id) in escalated_keys
        writes.add(fund.client_id, fund.unit_fund_id, ROUTE_ESCALATED_THIS_RUN, is_active)
    writes.flush(session)

    if run is None:
        return _close_run(session, run_to_use)
    return run_to_use


def _recompute_client_level(
    session: Session,
    as_of: date,
    run: SignalRun | None,
    clients_by_signal: dict[str, set[int]],
) -> SignalRun:
    run_to_use = run or _open_run(session)

    funds = session.execute(select(ActiveClientFund.client_id, ActiveClientFund.unit_fund_id)).all()

    writes = _SignalWrites(run_to_use.run_id, as_of)
    for fund in funds:
        for signal_code, client_ids in clients_by_signal.items():
            writes.add(fund.client_id, fund.unit_fund_id, signal_code, fund.client_id in client_ids)
    writes.flush(session)

    if run is None:
        return _close_run(session, run_to_use)
    return run_to_use


def _threshold_or_raise(session: Session, signal_code: str, name: str, as_of: date) -> float:
    value = active_threshold(session, signal_code, name, as_of)
    if value is None:
        raise SignalThresholdMissing(
            f"no {name} threshold for {signal_code} is in force on {as_of}"
        )
    return value


def recompute_tier_signals(
    session: Session, as_of: date, run: SignalRun | None = None
) -> SignalRun:
    tiered = session.execute(
        select(
            ClientTier.client_id, ClientTier.purchased_recently, ClientTier.real_withdrawer
        ).where(ClientTier.segment.is_not(None))
    ).all()
    return _recompute_client_level(
        session,
        as_of,
        run,
        {
            NO_RECENT_PURCHASE: {row.client_id for row in tiered if not row.purchased_recently},
            PURCHASED_THIS_PERIOD: {row.client_id for row in tiered if row.purchased_recently},
            REAL_WITHDRAWER: {row.client_id for row in tiered if row.real_withdrawer},
        },
    )


def recompute_past_typical_gap_signal(
    session: Session, as_of: date, run: SignalRun | None = None
) -> SignalRun:
    newest_deposit_per_client = session.execute(
        select(
            ActiveClientFund.client_id,
            ActiveClientFund.last_deposit_date,
            ActiveClientFund.typical_gap_days,
        )
        .where(ActiveClientFund.last_deposit_date.is_not(None))
        .distinct(ActiveClientFund.client_id)
        .order_by(
            ActiveClientFund.client_id,
            ActiveClientFund.last_deposit_date.desc(),
            ActiveClientFund.unit_fund_id,
        )
    ).all()
    past_gap = {
        row.client_id
        for row in newest_deposit_per_client
        if row.typical_gap_days is not None
        and (as_of - row.last_deposit_date).days > row.typical_gap_days
    }
    return _recompute_client_level(session, as_of, run, {PAST_TYPICAL_GAP: past_gap})


def _withdrawal_floor_and_fee(session: Session, as_of: date) -> tuple[float, float]:
    config = load_active_config(session, as_of)
    if config is None:
        raise RiskConfigMissing(f"no risk config version is in force on {as_of}")
    return float(config.thresholds["SYSTEM_FEE_MAX"]), float(config.thresholds["FEE_PER_MONTH"])


def _review_period_flows(
    session: Session, since: date, withdrawal_floor: float
) -> dict[int, tuple[float, float]]:
    deposits = func.coalesce(
        func.sum(ActiveTransaction.amount).filter(ActiveTransaction.txn_type == PURCHASE_TXN), 0.0
    )
    withdrawals = func.coalesce(
        func.sum(ActiveTransaction.amount).filter(
            ActiveTransaction.txn_type == SALE_TXN, ActiveTransaction.amount > withdrawal_floor
        ),
        0.0,
    )
    rows = session.execute(
        select(ActiveTransaction.client_id, deposits, withdrawals)
        .where(ActiveTransaction.txn_date >= since)
        .group_by(ActiveTransaction.client_id)
    ).all()
    return {client_id: (float(paid_in), float(taken_out)) for client_id, paid_in, taken_out in rows}


def recompute_net_flow_negative_signal(
    session: Session, as_of: date, run: SignalRun | None = None
) -> SignalRun:
    thresholds = load_tier_thresholds_in_force(session, as_of)
    withdrawal_floor, _ = _withdrawal_floor_and_fee(session, as_of)
    flows = _review_period_flows(
        session, as_of - timedelta(days=thresholds.review_period_days), withdrawal_floor
    )
    negative = {
        client_id
        for client_id, (paid_in, taken_out) in flows.items()
        if taken_out > 0 and taken_out >= paid_in
    }
    return _recompute_client_level(session, as_of, run, {NET_FLOW_NEGATIVE: negative})


def _is_heading_below_line(
    balance: float,
    funds_held: int,
    paid_in: float,
    taken_out: float,
    *,
    line: float,
    horizon_months: float,
    months_in_period: float,
    fee_per_month: float,
) -> bool:
    if balance < line:
        return False
    net_outflow_per_month = max(0.0, (taken_out - paid_in) / months_in_period)
    monthly_loss = net_outflow_per_month + fee_per_month * funds_held
    return balance - horizon_months * monthly_loss < line


def recompute_near_preservation_line_signal(
    session: Session, as_of: date, run: SignalRun | None = None
) -> SignalRun:
    horizon_months = _threshold_or_raise(session, NEAR_PRESERVATION_LINE, HORIZON_MONTHS, as_of)
    thresholds = load_tier_thresholds_in_force(session, as_of)
    withdrawal_floor, fee_per_month = _withdrawal_floor_and_fee(session, as_of)
    flows = _review_period_flows(
        session, as_of - timedelta(days=thresholds.review_period_days), withdrawal_floor
    )
    holdings = session.execute(
        select(
            ActiveClientFund.client_id,
            func.coalesce(func.sum(ActiveClientFund.balance), 0.0),
            func.count(),
        ).group_by(ActiveClientFund.client_id)
    ).all()

    months_in_period = thresholds.review_period_days * MONTHS_PER_YEAR / DAYS_PER_YEAR
    near_line = {
        client_id
        for client_id, balance, funds_held in holdings
        if _is_heading_below_line(
            float(balance),
            funds_held,
            *flows.get(client_id, (0.0, 0.0)),
            line=thresholds.capital_preservation_threshold_kes,
            horizon_months=horizon_months,
            months_in_period=months_in_period,
            fee_per_month=fee_per_month,
        )
    }
    return _recompute_client_level(session, as_of, run, {NEAR_PRESERVATION_LINE: near_line})


def recompute_side_changed_downward_signal(
    session: Session, as_of: date, run: SignalRun | None = None
) -> SignalRun:
    dropped = set(
        session.scalars(
            select(ClientSide.client_id).where(
                ClientSide.previous_side == SIDE_ACTIVE, ClientSide.side == SIDE_INACTIVE
            )
        )
    )
    return _recompute_client_level(session, as_of, run, {SIDE_CHANGED_DOWNWARD: dropped})


def recompute_segment_moved_up_signal(
    session: Session, as_of: date, run: SignalRun | None = None
) -> SignalRun:
    window_days = _threshold_or_raise(session, SEGMENT_MOVED_UP, WINDOW_DAYS, as_of)
    earliest = as_of - timedelta(days=int(window_days))
    now_active_active = session.execute(
        select(ClientTier.client_id, ClientTier.previous_tier, ClientTier.tier_changed_at).where(
            ClientTier.segment == SEGMENT_ACTIVE_ACTIVE,
            ClientTier.previous_tier.is_not(None),
            ClientTier.tier_changed_at.is_not(None),
        )
    ).all()
    moved_up = {
        row.client_id
        for row in now_active_active
        if SEGMENT_BY_TIER.get(row.previous_tier) == SEGMENT_ACTIVE_DORMANT
        and row.tier_changed_at.date() >= earliest
    }
    return _recompute_client_level(session, as_of, run, {SEGMENT_MOVED_UP: moved_up})


def recompute_all_signals(
    session: Session,
    as_of: date,
    months_until_empty_threshold: float,
    small_balance_threshold: float,
    awaiting_call_days: int,
    risk_run_id: str | None = None,
) -> SignalRun:
    run = _open_run(session)
    recompute_new_client_signals(session, as_of, run)
    recompute_fee_pressure_signal(session, as_of, months_until_empty_threshold, run)
    recompute_small_balance_signal(session, as_of, small_balance_threshold, run)
    recompute_single_fund_held_signal(session, as_of, run)
    recompute_call_follow_up_overdue_signal(session, as_of, awaiting_call_days, run)
    recompute_route_escalated_signal(session, as_of, risk_run_id, run)
    recompute_tier_signals(session, as_of, run)
    recompute_past_typical_gap_signal(session, as_of, run)
    recompute_net_flow_negative_signal(session, as_of, run)
    recompute_near_preservation_line_signal(session, as_of, run)
    recompute_side_changed_downward_signal(session, as_of, run)
    recompute_segment_moved_up_signal(session, as_of, run)
    return _close_run(session, run)
