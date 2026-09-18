from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import delete, select

from app.agents.signals import FIRST_DEPOSIT_RECENT, SINGLE_DEPOSIT, recompute_new_client_signals
from app.agents.situations import NEW_CLIENT_SINGLE_DEPOSIT, recompute_new_client_single_deposit
from app.db.models.active_clients import ActiveClientFund
from app.db.models.signals import (
    ClientSignalSnapshot,
    ClientSignalState,
    ClientSituationSnapshot,
    ClientSituationState,
)
from app.db.session import SessionLocal

FUND_ID = 9458
BOTH_ACTIVE_CLIENT = 945801
ONLY_RECENT_CLIENT = 945802
ONLY_SINGLE_CLIENT = 945803
NEITHER_CLIENT = 945804

CLIENT_IDS = (BOTH_ACTIVE_CLIENT, ONLY_RECENT_CLIENT, ONLY_SINGLE_CLIENT, NEITHER_CLIENT)

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
                _fund(BOTH_ACTIVE_CLIENT),
                _fund(ONLY_RECENT_CLIENT, n_deposits=3),
                _fund(
                    ONLY_SINGLE_CLIENT,
                    first_deposit_date=AS_OF - timedelta(days=WINDOW_DAYS + 1),
                ),
                _fund(
                    NEITHER_CLIENT,
                    n_deposits=5,
                    first_deposit_date=AS_OF - timedelta(days=WINDOW_DAYS + 1),
                ),
            ]
        )
        session.commit()

    yield

    with SessionLocal() as session:
        _purge(session)


def _state(session, client_id: int) -> ClientSituationState | None:
    return session.get(ClientSituationState, (client_id, FUND_ID, NEW_CLIENT_SINGLE_DEPOSIT))


def _recompute(session, as_of: date) -> str:
    run = recompute_new_client_signals(session, as_of)
    recompute_new_client_single_deposit(session, as_of, run.run_id)
    return run.run_id


def test_active_only_when_both_signals_are_active(book: None) -> None:
    with SessionLocal() as session:
        _recompute(session, AS_OF)

        both = _state(session, BOTH_ACTIVE_CLIENT)
        only_recent = _state(session, ONLY_RECENT_CLIENT)
        only_single = _state(session, ONLY_SINGLE_CLIENT)
        neither = _state(session, NEITHER_CLIENT)

    assert both.is_active is True
    assert sorted(both.signal_codes) == sorted([SINGLE_DEPOSIT, FIRST_DEPOSIT_RECENT])

    assert only_recent.is_active is False
    assert only_recent.signal_codes == [FIRST_DEPOSIT_RECENT]

    assert only_single.is_active is False
    assert only_single.signal_codes == [SINGLE_DEPOSIT]

    assert neither.is_active is False
    assert neither.signal_codes == []


def test_becomes_inactive_the_moment_either_signal_drops(book: None) -> None:
    first_as_of = AS_OF
    second_as_of = AS_OF + timedelta(days=1)

    with SessionLocal() as session:
        _recompute(session, first_as_of)
        first_since = _state(session, BOTH_ACTIVE_CLIENT).since

    with SessionLocal() as session:
        fund = session.get(ActiveClientFund, (BOTH_ACTIVE_CLIENT, FUND_ID))
        fund.n_deposits = 2
        session.commit()

    with SessionLocal() as session:
        _recompute(session, second_as_of)
        state = _state(session, BOTH_ACTIVE_CLIENT)

    assert state.is_active is False
    assert state.signal_codes == [FIRST_DEPOSIT_RECENT]
    assert state.since == second_as_of
    assert state.since != first_since


def test_snapshot_is_written_for_every_client_fund_the_signals_cover(book: None) -> None:
    with SessionLocal() as session:
        _recompute(session, AS_OF)
        rows = session.execute(
            select(ClientSituationSnapshot.client_id).where(
                ClientSituationSnapshot.client_id.in_(CLIENT_IDS)
            )
        ).all()

    assert {row.client_id for row in rows} == set(CLIENT_IDS)
