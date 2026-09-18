from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.agents import watchlist
from app.agents.signals import recompute_route_escalated_signal
from app.agents.situations import recompute_risk_action_gap
from app.agents.watchlist import WatchGroup, WatchlistThresholds, more_urgent_but_not_called
from app.db.models.active_clients import ActiveClientFund
from app.db.models.risk import RiskRun, RiskSnapshot
from app.db.models.signals import ClientSignalSnapshot, ClientSignalState, ClientSituationState
from app.db.session import SessionLocal

FUND_ID = 9558
ESCALATED_CLIENT = 955801
ON_CALL_LIST_CLIENT = 955802
MOVED_DOWN_CLIENT = 955803
UNCHANGED_CLIENT = 955804
FIRST_RUN_CLIENT = 955805

CLIENT_IDS = (
    ESCALATED_CLIENT,
    ON_CALL_LIST_CLIENT,
    MOVED_DOWN_CLIENT,
    UNCHANGED_CLIENT,
    FIRST_RUN_CLIENT,
)

RISK_RUN_ID = "more-urgent-parity-run"
EARLIER_RUN_ID = "more-urgent-parity-earlier-run"
AS_OF = date(2026, 9, 15)
CONFIG_VERSION = 1

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


def _snapshot(run_id: str, client_id: int, route: str | None) -> RiskSnapshot:
    return RiskSnapshot(
        run_id=run_id,
        client_id=client_id,
        unit_fund_id=FUND_ID,
        sig_heavy_withdrawal=False,
        sig_dormant=False,
        sig_broken_pattern=False,
        sig_shrinking=False,
        sig_going_dormant=False,
        sig_never_repeated=False,
        risk_score=50,
        risk_band="Watch",
        risk_reasons="no signal",
        fund_at_risk=0.0,
        config_version=CONFIG_VERSION,
        route=route,
    )


def _purge(session, client_ids: tuple[int, ...]) -> None:
    session.execute(
        delete(ClientSituationState).where(ClientSituationState.client_id.in_(client_ids))
    )
    session.execute(
        delete(ClientSignalSnapshot).where(ClientSignalSnapshot.client_id.in_(client_ids))
    )
    session.execute(delete(ClientSignalState).where(ClientSignalState.client_id.in_(client_ids)))
    session.execute(delete(RiskSnapshot).where(RiskSnapshot.client_id.in_(client_ids)))
    session.execute(delete(RiskRun).where(RiskRun.run_id.in_((RISK_RUN_ID, EARLIER_RUN_ID))))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
    session.commit()


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)
        session.add_all([_fund(client_id) for client_id in CLIENT_IDS])
        session.add(
            RiskRun(
                run_id=EARLIER_RUN_ID,
                state="completed",
                config_version=CONFIG_VERSION,
                finished_at=datetime(2026, 9, 13, 2, 0),
            )
        )
        session.add(
            RiskRun(
                run_id=RISK_RUN_ID,
                state="completed",
                config_version=CONFIG_VERSION,
                finished_at=datetime(2026, 9, 15, 2, 0),
            )
        )
        session.flush()
        session.add_all(
            [
                _snapshot(EARLIER_RUN_ID, ESCALATED_CLIENT, "monitor_only"),
                _snapshot(EARLIER_RUN_ID, ON_CALL_LIST_CLIENT, "auto_checkin"),
                _snapshot(EARLIER_RUN_ID, MOVED_DOWN_CLIENT, "fa_watchlist"),
                _snapshot(EARLIER_RUN_ID, UNCHANGED_CLIENT, "monitor_only"),
            ]
        )
        session.flush()
        session.add_all(
            [
                _snapshot(RISK_RUN_ID, ESCALATED_CLIENT, "fa_watchlist"),
                _snapshot(RISK_RUN_ID, ON_CALL_LIST_CLIENT, "fa_call_priority"),
                _snapshot(RISK_RUN_ID, MOVED_DOWN_CLIENT, "monitor_only"),
                _snapshot(RISK_RUN_ID, UNCHANGED_CLIENT, "monitor_only"),
                _snapshot(RISK_RUN_ID, FIRST_RUN_CLIENT, "fa_watchlist"),
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
    run = recompute_route_escalated_signal(session, AS_OF, RISK_RUN_ID)
    recompute_risk_action_gap(session, AS_OF, run.run_id)


def _group(monkeypatch, source: str) -> WatchGroup:
    monkeypatch.setattr(
        watchlist, "get_settings", lambda: SimpleNamespace(signal_situation_source=source)
    )
    with SessionLocal() as session:
        return more_urgent_but_not_called(session, THRESHOLDS, AS_OF, run_id=RISK_RUN_ID)


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
    assert _keys(situation) == {(ESCALATED_CLIENT, FUND_ID)}


@pytest.mark.parametrize("source", SOURCES)
def test_a_client_who_missed_the_call_list_is_included(
    book: None, monkeypatch, source: str
) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (ESCALATED_CLIENT, FUND_ID) in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_a_client_who_landed_on_the_call_list_is_excluded(
    book: None, monkeypatch, source: str
) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (ON_CALL_LIST_CLIENT, FUND_ID) not in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_a_client_who_moved_to_a_less_urgent_route_is_excluded(
    book: None, monkeypatch, source: str
) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (MOVED_DOWN_CLIENT, FUND_ID) not in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_a_client_whose_route_did_not_change_is_excluded(
    book: None, monkeypatch, source: str
) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (UNCHANGED_CLIENT, FUND_ID) not in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_a_first_ever_run_is_excluded(book: None, monkeypatch, source: str) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (FIRST_RUN_CLIENT, FUND_ID) not in _keys(group)
