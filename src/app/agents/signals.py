from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.signal_config import active_threshold
from app.db.bulk import copy_insert, copy_upsert
from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.digest import DigestLine, DigestRun
from app.db.models.signals import ClientSignalSnapshot, ClientSignalState, SignalRun
from app.risk.history import latest_completed_run_id, routes_before_run, routes_for_run
from app.risk.routing import route_direction

SINGLE_DEPOSIT = "single_deposit"
FIRST_DEPOSIT_RECENT = "first_deposit_recent"
WINDOW_DAYS = "window_days"

FEE_PRESSURE_CLOSE = "fee_pressure_close"
SMALL_BALANCE = "small_balance"
SINGLE_FUND_HELD = "single_fund_held"
CALL_FOLLOW_UP_OVERDUE = "call_follow_up_overdue"
ROUTE_ESCALATED_THIS_RUN = "route_escalated_this_run"

SIGNAL_CODES = (
    SINGLE_DEPOSIT,
    FIRST_DEPOSIT_RECENT,
    FEE_PRESSURE_CLOSE,
    SMALL_BALANCE,
    SINGLE_FUND_HELD,
    CALL_FOLLOW_UP_OVERDUE,
    ROUTE_ESCALATED_THIS_RUN,
)

ONE_DEPOSIT = 1
ONE_FUND = 1

CALL_LIST_ROUTE = "fa_call_priority"
MORE_URGENT = "more_urgent"


class NewClientWindowMissing(LookupError):
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
    return _close_run(session, run)
