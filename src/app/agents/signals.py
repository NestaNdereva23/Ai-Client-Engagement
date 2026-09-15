from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from app.agents.signal_config import active_threshold
from app.db.models.active_clients import ActiveClientFund
from app.db.models.signals import ClientSignalSnapshot, ClientSignalState, SignalRun

SINGLE_DEPOSIT = "single_deposit"
FIRST_DEPOSIT_RECENT = "first_deposit_recent"
WINDOW_DAYS = "window_days"

SIGNAL_CODES = (SINGLE_DEPOSIT, FIRST_DEPOSIT_RECENT)

ONE_DEPOSIT = 1


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


def _existing_state(
    session: Session, keys: list[tuple[int, int]]
) -> dict[tuple[int, int, str], ClientSignalState]:
    rows = session.scalars(
        select(ClientSignalState).where(
            tuple_(ClientSignalState.client_id, ClientSignalState.unit_fund_id).in_(keys)
        )
    )
    return {(row.client_id, row.unit_fund_id, row.signal_code): row for row in rows}


def recompute_new_client_signals(session: Session, as_of: date) -> SignalRun:
    run = SignalRun(run_id=uuid4().hex)
    session.add(run)
    session.flush()

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

    existing = _existing_state(session, [(fund.client_id, fund.unit_fund_id) for fund in funds])

    for fund in funds:
        values = _signal_values(fund.n_deposits, fund.first_deposit_date, as_of, int(window_days))
        for signal_code, is_active in values.items():
            session.add(
                ClientSignalSnapshot(
                    run_id=run.run_id,
                    client_id=fund.client_id,
                    unit_fund_id=fund.unit_fund_id,
                    signal_code=signal_code,
                    is_active=is_active,
                )
            )
            prior = existing.get((fund.client_id, fund.unit_fund_id, signal_code))
            if prior is None:
                session.add(
                    ClientSignalState(
                        client_id=fund.client_id,
                        unit_fund_id=fund.unit_fund_id,
                        signal_code=signal_code,
                        is_active=is_active,
                        since=as_of,
                        run_id=run.run_id,
                    )
                )
            else:
                if prior.is_active != is_active:
                    prior.since = as_of
                prior.is_active = is_active
                prior.run_id = run.run_id

    run.state = "completed"
    run.finished_at = func.now()
    session.commit()
    return run
