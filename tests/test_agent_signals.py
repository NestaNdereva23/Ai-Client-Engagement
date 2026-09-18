from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import delete, select

from app.agents.signals import (
    FIRST_DEPOSIT_RECENT,
    SINGLE_DEPOSIT,
    recompute_new_client_signals,
)
from app.db.models.active_clients import ActiveClientFund
from app.db.models.signals import ClientSignalSnapshot, ClientSignalState
from app.db.session import SessionLocal

FUND_ID = 9457
INSIDE_CLIENT = 945701
OUTSIDE_CLIENT = 945702
REPEAT_CLIENT = 945703
NEVER_CLIENT = 945704
FLIP_CLIENT = 945705

CLIENT_IDS = (INSIDE_CLIENT, OUTSIDE_CLIENT, REPEAT_CLIENT, NEVER_CLIENT, FLIP_CLIENT)

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
                _fund(INSIDE_CLIENT),
                _fund(OUTSIDE_CLIENT, first_deposit_date=AS_OF - timedelta(days=WINDOW_DAYS + 1)),
                _fund(REPEAT_CLIENT, n_deposits=4),
                _fund(NEVER_CLIENT, n_deposits=0, first_deposit_date=None),
                _fund(FLIP_CLIENT),
            ]
        )
        session.commit()

    yield

    with SessionLocal() as session:
        _purge(session)


def _state(session, client_id: int, signal_code: str) -> ClientSignalState | None:
    return session.get(ClientSignalState, (client_id, FUND_ID, signal_code))


def test_a_deposit_inside_the_window_activates_both_signals(book: None) -> None:
    with SessionLocal() as session:
        run = recompute_new_client_signals(session, AS_OF)
        single = _state(session, INSIDE_CLIENT, SINGLE_DEPOSIT)
        recent = _state(session, INSIDE_CLIENT, FIRST_DEPOSIT_RECENT)
    assert run.state == "completed"
    assert single.is_active is True
    assert recent.is_active is True
    assert single.since == AS_OF
    assert recent.since == AS_OF


def test_a_deposit_outside_the_window_leaves_first_deposit_recent_inactive(book: None) -> None:
    with SessionLocal() as session:
        recompute_new_client_signals(session, AS_OF)
        single = _state(session, OUTSIDE_CLIENT, SINGLE_DEPOSIT)
        recent = _state(session, OUTSIDE_CLIENT, FIRST_DEPOSIT_RECENT)
    assert single.is_active is True
    assert recent.is_active is False


def test_several_deposits_leaves_single_deposit_inactive(book: None) -> None:
    with SessionLocal() as session:
        recompute_new_client_signals(session, AS_OF)
        single = _state(session, REPEAT_CLIENT, SINGLE_DEPOSIT)
    assert single.is_active is False


def test_no_deposits_leaves_both_signals_inactive(book: None) -> None:
    with SessionLocal() as session:
        recompute_new_client_signals(session, AS_OF)
        single = _state(session, NEVER_CLIENT, SINGLE_DEPOSIT)
        recent = _state(session, NEVER_CLIENT, FIRST_DEPOSIT_RECENT)
    assert single.is_active is False
    assert recent.is_active is False


def test_a_second_run_on_unchanged_data_leaves_since_untouched(book: None) -> None:
    with SessionLocal() as session:
        recompute_new_client_signals(session, AS_OF)
        first_since = _state(session, INSIDE_CLIENT, SINGLE_DEPOSIT).since

    with SessionLocal() as session:
        second_run = recompute_new_client_signals(session, AS_OF + timedelta(days=1))
        state = _state(session, INSIDE_CLIENT, SINGLE_DEPOSIT)

    assert state.since == first_since
    assert state.run_id == second_run.run_id


def test_a_second_deposit_between_runs_is_provable_from_snapshot_history(book: None) -> None:
    second_as_of = AS_OF + timedelta(days=1)

    with SessionLocal() as session:
        first_run = recompute_new_client_signals(session, AS_OF)

    with SessionLocal() as session:
        fund = session.get(ActiveClientFund, (FLIP_CLIENT, FUND_ID))
        fund.n_deposits = 2
        session.commit()

    with SessionLocal() as session:
        second_run = recompute_new_client_signals(session, second_as_of)
        state = _state(session, FLIP_CLIENT, SINGLE_DEPOSIT)

        first_snapshot = session.scalar(
            select(ClientSignalSnapshot).where(
                ClientSignalSnapshot.run_id == first_run.run_id,
                ClientSignalSnapshot.client_id == FLIP_CLIENT,
                ClientSignalSnapshot.signal_code == SINGLE_DEPOSIT,
            )
        )
        second_snapshot = session.scalar(
            select(ClientSignalSnapshot).where(
                ClientSignalSnapshot.run_id == second_run.run_id,
                ClientSignalSnapshot.client_id == FLIP_CLIENT,
                ClientSignalSnapshot.signal_code == SINGLE_DEPOSIT,
            )
        )

    assert first_snapshot.is_active is True
    assert second_snapshot.is_active is False
    assert state.is_active is False
    assert state.since == second_as_of


def test_recompute_only_writes_the_two_new_client_signals(book: None) -> None:
    with SessionLocal() as session:
        recompute_new_client_signals(session, AS_OF)
        codes = set(
            session.scalars(
                select(ClientSignalState.signal_code).where(
                    ClientSignalState.client_id.in_(CLIENT_IDS)
                )
            )
        )
    assert codes == {SINGLE_DEPOSIT, FIRST_DEPOSIT_RECENT}
