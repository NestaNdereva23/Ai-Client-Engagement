from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.agents import watchlist
from app.agents.signals import recompute_small_balance_signal
from app.agents.situations import recompute_small_balance_inactive
from app.agents.watchlist import WatchGroup, WatchlistThresholds, very_small_and_quiet
from app.db.models.active_clients import ActiveClientFund
from app.db.models.risk import ClientRiskFeatures
from app.db.models.signals import ClientSignalSnapshot, ClientSignalState, ClientSituationState
from app.db.session import SessionLocal

FUND_ID = 9553
QUIET_CLIENT = 955301
MOVING_CLIENT = 955302
LARGE_BALANCE_CLIENT = 955303
NO_RISK_ROW_CLIENT = 955304

CLIENT_IDS = (
    QUIET_CLIENT,
    MOVING_CLIENT,
    LARGE_BALANCE_CLIENT,
    NO_RISK_ROW_CLIENT,
)

AS_OF = date(2026, 9, 15)
SMALL_BALANCE = 100.0

THRESHOLDS = WatchlistThresholds(
    new_client_days=30, months_until_empty=6.0, small_balance=SMALL_BALANCE, awaiting_call_days=2
)

SOURCES = ("legacy", "situations")


def _fund(client_id: int, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance=40.0,
        n_deposits=5,
        n_withdrawals=0,
    )
    row.update(overrides)
    return ActiveClientFund(**row)


def _risk(client_id: int, **overrides) -> ClientRiskFeatures:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        sig_heavy_withdrawal=False,
        sig_dormant=True,
        sig_broken_pattern=False,
        sig_shrinking=False,
        sig_going_dormant=False,
        sig_never_repeated=False,
        risk_score=10,
        risk_band="Low",
        risk_reasons="no signal",
        fund_at_risk=0.0,
        config_version=1,
    )
    row.update(overrides)
    return ClientRiskFeatures(**row)


def _purge(session, client_ids: tuple[int, ...]) -> None:
    session.execute(
        delete(ClientSituationState).where(ClientSituationState.client_id.in_(client_ids))
    )
    session.execute(
        delete(ClientSignalSnapshot).where(ClientSignalSnapshot.client_id.in_(client_ids))
    )
    session.execute(delete(ClientSignalState).where(ClientSignalState.client_id.in_(client_ids)))
    session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(client_ids)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
    session.commit()


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)
        session.add_all(
            [
                _fund(QUIET_CLIENT, balance=40.0),
                _fund(MOVING_CLIENT, balance=40.0),
                _fund(LARGE_BALANCE_CLIENT, balance=500_000.0),
                _fund(NO_RISK_ROW_CLIENT, balance=40.0),
            ]
        )
        session.add_all(
            [
                _risk(QUIET_CLIENT, sig_dormant=True),
                _risk(MOVING_CLIENT, sig_dormant=False),
                _risk(LARGE_BALANCE_CLIENT, sig_dormant=True),
            ]
        )
        session.commit()

    yield

    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)


def _keys(group: WatchGroup) -> set[tuple[int, int]]:
    return {(member.client_id, member.unit_fund_id) for member in group.members}


def _money_total(group: WatchGroup) -> float:
    return sum(member.balance for member in group.members)


def _recompute(session) -> None:
    run = recompute_small_balance_signal(session, AS_OF, SMALL_BALANCE)
    recompute_small_balance_inactive(session, AS_OF, run.run_id)


def _group(monkeypatch, source: str) -> WatchGroup:
    monkeypatch.setattr(
        watchlist, "get_settings", lambda: SimpleNamespace(signal_situation_source=source)
    )
    with SessionLocal() as session:
        return very_small_and_quiet(session, THRESHOLDS, AS_OF)


def test_the_two_sources_agree_on_client_funds_counts_and_money_total(
    book: None, monkeypatch
) -> None:
    with SessionLocal() as session:
        _recompute(session)

    legacy = _group(monkeypatch, "legacy")
    situation = _group(monkeypatch, "situations")

    assert _keys(legacy) == _keys(situation)
    assert len(legacy.members) == len(situation.members)
    assert _money_total(legacy) == _money_total(situation)
    assert _keys(situation) == {(QUIET_CLIENT, FUND_ID)}


@pytest.mark.parametrize("source", SOURCES)
def test_a_small_balance_that_has_stopped_moving_is_included(
    book: None, monkeypatch, source: str
) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (QUIET_CLIENT, FUND_ID) in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_a_small_balance_that_still_moves_is_excluded(book: None, monkeypatch, source: str) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (MOVING_CLIENT, FUND_ID) not in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_a_large_balance_is_excluded(book: None, monkeypatch, source: str) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (LARGE_BALANCE_CLIENT, FUND_ID) not in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_a_client_with_no_risk_row_is_excluded(book: None, monkeypatch, source: str) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (NO_RISK_ROW_CLIENT, FUND_ID) not in _keys(group)
