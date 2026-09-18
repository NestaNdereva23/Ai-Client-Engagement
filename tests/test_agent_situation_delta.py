from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import delete

from app.agents.signals import FIRST_DEPOSIT_RECENT, SINGLE_DEPOSIT, recompute_new_client_signals
from app.agents.situations import (
    NEW_CLIENT_SINGLE_DEPOSIT,
    recompute_new_client_single_deposit,
    resolved_signal_codes,
    situation_delta,
)
from app.db.models.active_clients import ActiveClientFund
from app.db.models.signals import (
    ClientSignalSnapshot,
    ClientSignalState,
    ClientSituationSnapshot,
    ClientSituationState,
)
from app.db.session import SessionLocal

FUND_ID = 9459
FIRST_TIME_CLIENT = 945901
SECOND_DEPOSIT_CLIENT = 945902
WINDOW_CLIENT = 945903
PERSIST_CLIENT = 945904

CLIENT_IDS = (FIRST_TIME_CLIENT, SECOND_DEPOSIT_CLIENT, WINDOW_CLIENT, PERSIST_CLIENT)

AS_OF = date(2026, 9, 15)
WINDOW_DAYS = 30


@pytest.fixture(autouse=True)
def fixed_window(monkeypatch):
    from app.agents import signals

    monkeypatch.setattr(signals, "active_threshold", lambda *args, **kwargs: WINDOW_DAYS)


def _fund(client_id: int, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance=100_000.0,
        n_deposits=1,
        n_withdrawals=0,
        first_deposit_date=AS_OF - timedelta(days=3),
    )
    row.update(overrides)
    return ActiveClientFund(**row)


def _purge(session) -> None:
    session.execute(
        delete(ClientSituationSnapshot).where(ClientSituationSnapshot.client_id.in_(CLIENT_IDS))
    )
    session.execute(
        delete(ClientSituationState).where(ClientSituationState.client_id.in_(CLIENT_IDS))
    )
    session.execute(
        delete(ClientSignalSnapshot).where(ClientSignalSnapshot.client_id.in_(CLIENT_IDS))
    )
    session.execute(delete(ClientSignalState).where(ClientSignalState.client_id.in_(CLIENT_IDS)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(CLIENT_IDS)))
    session.commit()


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session)
        session.add_all(
            [
                _fund(FIRST_TIME_CLIENT),
                _fund(SECOND_DEPOSIT_CLIENT),
                _fund(WINDOW_CLIENT),
                _fund(PERSIST_CLIENT),
            ]
        )
        session.commit()

    yield

    with SessionLocal() as session:
        _purge(session)


def _recompute(session, as_of: date) -> str:
    run = recompute_new_client_signals(session, as_of)
    recompute_new_client_single_deposit(session, as_of, run.run_id)
    return run.run_id


def test_a_client_fund_appearing_for_the_first_time_is_newly_active(book: None) -> None:
    with SessionLocal() as session:
        run_id = _recompute(session, AS_OF)
        delta = situation_delta(session, NEW_CLIENT_SINGLE_DEPOSIT, run_id)

    assert (FIRST_TIME_CLIENT, FUND_ID) in delta.newly_active
    assert (FIRST_TIME_CLIENT, FUND_ID) not in delta.newly_inactive
    assert (FIRST_TIME_CLIENT, FUND_ID) not in delta.persisting


def test_a_client_fund_resolving_by_a_second_deposit(book: None) -> None:
    with SessionLocal() as session:
        _recompute(session, AS_OF)

    with SessionLocal() as session:
        fund = session.get(ActiveClientFund, (SECOND_DEPOSIT_CLIENT, FUND_ID))
        fund.n_deposits = 2
        session.commit()

    with SessionLocal() as session:
        second_run_id = _recompute(session, AS_OF + timedelta(days=1))
        delta = situation_delta(session, NEW_CLIENT_SINGLE_DEPOSIT, second_run_id)
        reason = resolved_signal_codes(session, SECOND_DEPOSIT_CLIENT, FUND_ID, second_run_id)

    assert (SECOND_DEPOSIT_CLIENT, FUND_ID) in delta.newly_inactive
    assert reason == (SINGLE_DEPOSIT,)


def test_a_client_fund_resolving_by_the_window_elapsing(book: None) -> None:
    with SessionLocal() as session:
        _recompute(session, AS_OF)

    with SessionLocal() as session:
        second_run_id = _recompute(session, AS_OF + timedelta(days=WINDOW_DAYS + 10))
        delta = situation_delta(session, NEW_CLIENT_SINGLE_DEPOSIT, second_run_id)
        reason = resolved_signal_codes(session, WINDOW_CLIENT, FUND_ID, second_run_id)

    assert (WINDOW_CLIENT, FUND_ID) in delta.newly_inactive
    assert reason == (FIRST_DEPOSIT_RECENT,)


def test_a_client_fund_persisting_across_three_runs_is_unchanged(book: None) -> None:
    with SessionLocal() as session:
        first_run_id = _recompute(session, AS_OF)
        first_delta = situation_delta(session, NEW_CLIENT_SINGLE_DEPOSIT, first_run_id)

    with SessionLocal() as session:
        second_run_id = _recompute(session, AS_OF + timedelta(days=1))
        second_delta = situation_delta(session, NEW_CLIENT_SINGLE_DEPOSIT, second_run_id)

    with SessionLocal() as session:
        third_run_id = _recompute(session, AS_OF + timedelta(days=2))
        third_delta = situation_delta(session, NEW_CLIENT_SINGLE_DEPOSIT, third_run_id)

    assert (PERSIST_CLIENT, FUND_ID) in first_delta.newly_active
    assert (PERSIST_CLIENT, FUND_ID) in second_delta.persisting
    assert (PERSIST_CLIENT, FUND_ID) not in second_delta.newly_active
    assert (PERSIST_CLIENT, FUND_ID) in third_delta.persisting
    assert (PERSIST_CLIENT, FUND_ID) not in third_delta.newly_active
