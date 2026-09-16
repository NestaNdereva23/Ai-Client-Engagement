from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.agents import watchlist
from app.agents.situations import recompute_contribution_decline
from app.agents.watchlist import WatchGroup, WatchlistThresholds, getting_smaller
from app.db.models.active_clients import ActiveClientFund
from app.db.models.risk import ClientRiskFeatures
from app.db.models.signals import ClientSituationSnapshot, ClientSituationState, SignalRun
from app.db.session import SessionLocal

FUND_ID = 9554
SHRINKING_CLIENT = 955401
STABLE_CLIENT = 955402
NO_RISK_ROW_CLIENT = 955403

CLIENT_IDS = (SHRINKING_CLIENT, STABLE_CLIENT, NO_RISK_ROW_CLIENT)

RUN_ID = "getting-smaller-parity-run"
AS_OF = date(2026, 9, 15)

THRESHOLDS = WatchlistThresholds(
    new_client_days=30, months_until_empty=6.0, small_balance=100.0, awaiting_call_days=2
)

SOURCES = ("legacy", "situations")


def _fund(client_id: int, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance=200_000.0,
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
        sig_dormant=False,
        sig_broken_pattern=False,
        sig_shrinking=False,
        sig_going_dormant=False,
        sig_never_repeated=False,
        risk_score=10,
        risk_band="Watch",
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
        delete(ClientSituationSnapshot).where(ClientSituationSnapshot.client_id.in_(client_ids))
    )
    session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(client_ids)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
    session.execute(delete(SignalRun).where(SignalRun.run_id == RUN_ID))
    session.commit()


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)
        session.add_all(
            [
                _fund(SHRINKING_CLIENT),
                _fund(STABLE_CLIENT),
                _fund(NO_RISK_ROW_CLIENT),
            ]
        )
        session.add_all(
            [
                _risk(SHRINKING_CLIENT, sig_shrinking=True),
                _risk(STABLE_CLIENT, sig_shrinking=False),
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
    session.add(SignalRun(run_id=RUN_ID, state="completed"))
    session.flush()
    recompute_contribution_decline(session, AS_OF, RUN_ID)


def _group(monkeypatch, source: str) -> WatchGroup:
    monkeypatch.setattr(
        watchlist, "get_settings", lambda: SimpleNamespace(signal_situation_source=source)
    )
    with SessionLocal() as session:
        return getting_smaller(session, THRESHOLDS, AS_OF)


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
    assert _keys(situation) == {(SHRINKING_CLIENT, FUND_ID)}


@pytest.mark.parametrize("source", SOURCES)
def test_shrinking_deposits_are_included(book: None, monkeypatch, source: str) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (SHRINKING_CLIENT, FUND_ID) in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_stable_deposits_are_excluded(book: None, monkeypatch, source: str) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (STABLE_CLIENT, FUND_ID) not in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_a_client_with_no_risk_row_is_excluded(book: None, monkeypatch, source: str) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (NO_RISK_ROW_CLIENT, FUND_ID) not in _keys(group)
