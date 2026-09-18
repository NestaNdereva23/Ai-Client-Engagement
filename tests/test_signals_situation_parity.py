from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.agents import signals, watchlist
from app.agents.signals import recompute_new_client_signals
from app.agents.situations import recompute_new_client_single_deposit
from app.agents.watchlist import WatchGroup, WatchlistThresholds, signed_up_recently
from app.db.models.active_clients import ActiveClientFund
from app.db.models.signals import ClientSignalSnapshot, ClientSignalState, ClientSituationState
from app.db.session import SessionLocal

FUND_ID = 9550
LAST_VALID_DAY_CLIENT = 955001
TWO_DEPOSIT_CLIENT = 955002
ZERO_DEPOSIT_CLIENT = 955003
NULL_FIRST_DEPOSIT_CLIENT = 955004

CLIENT_IDS = (
    LAST_VALID_DAY_CLIENT,
    TWO_DEPOSIT_CLIENT,
    ZERO_DEPOSIT_CLIENT,
    NULL_FIRST_DEPOSIT_CLIENT,
)

MOVING_WINDOW_FUND_ID = 9551
OUT_OF_OLD_WINDOW_CLIENT = 955101

MOVING_WINDOW_CLIENT_IDS = (OUT_OF_OLD_WINDOW_CLIENT,)

AS_OF = date(2026, 9, 15)
WINDOW_DAYS = 30

THRESHOLDS = WatchlistThresholds(
    new_client_days=WINDOW_DAYS, months_until_empty=6.0, small_balance=100.0, awaiting_call_days=2
)

SOURCES = ("legacy", "situations")


def _fund(client_id: int, unit_fund_id: int, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        balance=100_000.0,
        n_deposits=1,
        n_withdrawals=0,
        first_deposit_date=AS_OF - timedelta(days=3),
    )
    row.update(overrides)
    return ActiveClientFund(**row)


def _purge(session, client_ids: tuple[int, ...]) -> None:
    session.execute(
        delete(ClientSituationState).where(ClientSituationState.client_id.in_(client_ids))
    )
    session.execute(
        delete(ClientSignalSnapshot).where(ClientSignalSnapshot.client_id.in_(client_ids))
    )
    session.execute(delete(ClientSignalState).where(ClientSignalState.client_id.in_(client_ids)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
    session.commit()


@pytest.fixture
def boundary_book(db: None):
    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)
        session.add_all(
            [
                _fund(
                    LAST_VALID_DAY_CLIENT,
                    FUND_ID,
                    balance=120_000.0,
                    n_deposits=1,
                    first_deposit_date=AS_OF - timedelta(days=WINDOW_DAYS),
                ),
                _fund(
                    TWO_DEPOSIT_CLIENT,
                    FUND_ID,
                    balance=200_000.0,
                    n_deposits=2,
                    first_deposit_date=AS_OF - timedelta(days=3),
                ),
                _fund(
                    ZERO_DEPOSIT_CLIENT,
                    FUND_ID,
                    balance=0.0,
                    n_deposits=0,
                    first_deposit_date=None,
                ),
                _fund(
                    NULL_FIRST_DEPOSIT_CLIENT,
                    FUND_ID,
                    balance=50_000.0,
                    n_deposits=1,
                    first_deposit_date=None,
                ),
            ]
        )
        session.commit()

    yield

    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)


@pytest.fixture
def fixed_window(monkeypatch):
    monkeypatch.setattr(signals, "active_threshold", lambda *args, **kwargs: WINDOW_DAYS)


def _keys(group: WatchGroup) -> set[tuple[int, int]]:
    return {(member.client_id, member.unit_fund_id) for member in group.members}


def _money_total(group: WatchGroup) -> float:
    return sum(member.balance for member in group.members)


def _group(monkeypatch, source: str, thresholds: WatchlistThresholds = THRESHOLDS) -> WatchGroup:
    monkeypatch.setattr(
        watchlist, "get_settings", lambda: SimpleNamespace(signal_situation_source=source)
    )
    with SessionLocal() as session:
        return signed_up_recently(session, thresholds, AS_OF)


def test_the_two_sources_agree_on_client_funds_counts_and_money_total(
    boundary_book: None, fixed_window: None, monkeypatch
) -> None:
    with SessionLocal() as session:
        run = recompute_new_client_signals(session, AS_OF)
        recompute_new_client_single_deposit(session, AS_OF, run.run_id)

    legacy = _group(monkeypatch, "legacy")
    situation = _group(monkeypatch, "situations")

    assert _keys(legacy) == _keys(situation)
    assert len(legacy.members) == len(situation.members)
    assert _money_total(legacy) == _money_total(situation)
    assert _keys(situation) == {(LAST_VALID_DAY_CLIENT, FUND_ID)}


@pytest.mark.parametrize("source", SOURCES)
def test_a_deposit_on_the_last_valid_day_is_included(
    boundary_book: None, fixed_window: None, monkeypatch, source: str
) -> None:
    with SessionLocal() as session:
        run = recompute_new_client_signals(session, AS_OF)
        recompute_new_client_single_deposit(session, AS_OF, run.run_id)

    group = _group(monkeypatch, source)

    assert (LAST_VALID_DAY_CLIENT, FUND_ID) in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_a_second_deposit_excludes_the_client(
    boundary_book: None, fixed_window: None, monkeypatch, source: str
) -> None:
    with SessionLocal() as session:
        run = recompute_new_client_signals(session, AS_OF)
        recompute_new_client_single_deposit(session, AS_OF, run.run_id)

    group = _group(monkeypatch, source)

    assert (TWO_DEPOSIT_CLIENT, FUND_ID) not in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_zero_deposits_excludes_the_client(
    boundary_book: None, fixed_window: None, monkeypatch, source: str
) -> None:
    with SessionLocal() as session:
        run = recompute_new_client_signals(session, AS_OF)
        recompute_new_client_single_deposit(session, AS_OF, run.run_id)

    group = _group(monkeypatch, source)

    assert (ZERO_DEPOSIT_CLIENT, FUND_ID) not in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_a_null_first_deposit_date_excludes_the_client(
    boundary_book: None, fixed_window: None, monkeypatch, source: str
) -> None:
    with SessionLocal() as session:
        run = recompute_new_client_signals(session, AS_OF)
        recompute_new_client_single_deposit(session, AS_OF, run.run_id)

    group = _group(monkeypatch, source)

    assert (NULL_FIRST_DEPOSIT_CLIENT, FUND_ID) not in _keys(group)


@pytest.fixture
def moving_window_book(db: None):
    with SessionLocal() as session:
        _purge(session, MOVING_WINDOW_CLIENT_IDS)
        session.add(
            _fund(
                OUT_OF_OLD_WINDOW_CLIENT,
                MOVING_WINDOW_FUND_ID,
                balance=75_000.0,
                n_deposits=1,
                first_deposit_date=AS_OF - timedelta(days=40),
            )
        )
        session.commit()

    yield

    with SessionLocal() as session:
        _purge(session, MOVING_WINDOW_CLIENT_IDS)


def test_widening_the_window_moves_both_sources_together(
    moving_window_book: None, monkeypatch
) -> None:
    narrow = WatchlistThresholds(
        new_client_days=30, months_until_empty=6.0, small_balance=100.0, awaiting_call_days=2
    )
    wide = WatchlistThresholds(
        new_client_days=45, months_until_empty=6.0, small_balance=100.0, awaiting_call_days=2
    )

    monkeypatch.setattr(signals, "active_threshold", lambda *args, **kwargs: 30)
    with SessionLocal() as session:
        run = recompute_new_client_signals(session, AS_OF)
        recompute_new_client_single_deposit(session, AS_OF, run.run_id)

    for source in SOURCES:
        group = _group(monkeypatch, source, narrow)
        assert (OUT_OF_OLD_WINDOW_CLIENT, MOVING_WINDOW_FUND_ID) not in _keys(group)

    monkeypatch.setattr(signals, "active_threshold", lambda *args, **kwargs: 45)
    with SessionLocal() as session:
        run = recompute_new_client_signals(session, AS_OF)
        recompute_new_client_single_deposit(session, AS_OF, run.run_id)

    legacy = _group(monkeypatch, "legacy", wide)
    situation = _group(monkeypatch, "situations", wide)

    assert (OUT_OF_OLD_WINDOW_CLIENT, MOVING_WINDOW_FUND_ID) in _keys(legacy)
    assert (OUT_OF_OLD_WINDOW_CLIENT, MOVING_WINDOW_FUND_ID) in _keys(situation)
    assert _keys(legacy) == _keys(situation)
