"""The nightly watch list: one group per active situation that has an action, read from
the situation table, plus the empty case and the threshold loader."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.agents import watchlist
from app.agents.watchlist import (
    GROUP_NAMES,
    GROUP_TO_SITUATION,
    WatchlistConfigMissing,
    WatchlistThresholds,
    build_watchlist,
    situation_group,
)
from app.db.models.active_clients import ActiveClientFund
from app.db.models.risk import RiskConfigVersion
from app.db.models.signals import ClientSituationState, SignalRun
from app.db.session import SessionLocal

AS_OF = date(2026, 10, 13)
FUND_ID = 98081
FIRST_CLIENT = 980801
SIGNAL_RUN_ID = "watchlist-test-signal-run"
CLIENT_IDS = tuple(range(FIRST_CLIENT, FIRST_CLIENT + len(GROUP_NAMES) + 2))

THRESHOLDS = WatchlistThresholds(
    new_client_days=30, months_until_empty=6.0, small_balance=100.0, awaiting_call_days=2
)


def _purge(session) -> None:
    session.execute(
        delete(ClientSituationState).where(ClientSituationState.client_id.in_(CLIENT_IDS))
    )
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(CLIENT_IDS)))
    session.execute(delete(SignalRun).where(SignalRun.run_id == SIGNAL_RUN_ID))
    session.commit()


def _client_for(group_name: str) -> int:
    return FIRST_CLIENT + GROUP_NAMES.index(group_name)


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session)
        session.add(SignalRun(run_id=SIGNAL_RUN_ID, state="completed"))
        session.flush()
        for group_name in GROUP_NAMES:
            session.add(
                ActiveClientFund(
                    client_id=_client_for(group_name),
                    unit_fund_id=FUND_ID,
                    balance=10_000.0,
                    n_deposits=3,
                    n_withdrawals=0,
                )
            )
            session.add(
                ClientSituationState(
                    client_id=_client_for(group_name),
                    unit_fund_id=FUND_ID,
                    situation_code=GROUP_TO_SITUATION[group_name],
                    is_active=True,
                    signal_codes=[],
                    since=AS_OF,
                    run_id=SIGNAL_RUN_ID,
                )
            )
        inactive = FIRST_CLIENT + len(GROUP_NAMES)
        session.add(
            ActiveClientFund(
                client_id=inactive,
                unit_fund_id=FUND_ID,
                balance=500.0,
                n_deposits=3,
                n_withdrawals=0,
            )
        )
        session.add(
            ClientSituationState(
                client_id=inactive,
                unit_fund_id=FUND_ID,
                situation_code=GROUP_TO_SITUATION[GROUP_NAMES[0]],
                is_active=False,
                signal_codes=[],
                since=AS_OF,
                run_id=SIGNAL_RUN_ID,
            )
        )
        session.commit()
    yield
    with SessionLocal() as session:
        _purge(session)


def _members(group) -> set[int]:
    return {member.client_id for member in group.members} & set(CLIENT_IDS)


@pytest.mark.parametrize("group_name", GROUP_NAMES)
def test_each_group_finds_the_client_with_its_situation_active(book, group_name) -> None:
    with SessionLocal() as session:
        group = situation_group(session, group_name)

    assert group.name == group_name
    assert _members(group) == {_client_for(group_name)}
    assert group.definition == {"situation_code": GROUP_TO_SITUATION[group_name], "is_active": True}


def test_a_client_whose_situation_is_inactive_is_left_out(book) -> None:
    inactive = FIRST_CLIENT + len(GROUP_NAMES)
    with SessionLocal() as session:
        group = situation_group(session, GROUP_NAMES[0])

    assert inactive not in _members(group)


def test_a_group_counts_clients_funds_and_money(book) -> None:
    with SessionLocal() as session:
        group = situation_group(session, GROUP_NAMES[0])

    assert group.client_count >= 1
    assert group.fund_count >= group.client_count
    assert group.money_total >= 10_000.0


def test_a_group_carries_no_client_name(book) -> None:
    with SessionLocal() as session:
        group = situation_group(session, GROUP_NAMES[0])

    assert {field for field in group.members[0].__dataclass_fields__} == {
        "client_id",
        "unit_fund_id",
        "balance",
    }


def test_build_watchlist_returns_every_group_in_order(book) -> None:
    with SessionLocal() as session:
        groups = build_watchlist(session, AS_OF, THRESHOLDS)

    assert [group.name for group in groups] == list(GROUP_NAMES)


def test_a_group_with_nobody_in_it_is_still_returned(db: None) -> None:
    with SessionLocal() as session:
        _purge(session)
        groups = build_watchlist(session, AS_OF, THRESHOLDS)

    assert len(groups) == len(GROUP_NAMES)
    assert all(_members(group) == set() for group in groups)


def test_every_group_is_named_after_a_situation_that_exists() -> None:
    from app.agents.situations import SITUATION_CODES

    assert set(GROUP_TO_SITUATION.values()) <= set(SITUATION_CODES)


def test_load_thresholds_reads_settings_and_the_risk_config(monkeypatch) -> None:
    monkeypatch.setattr(
        watchlist,
        "load_active_config",
        lambda session, at: RiskConfigVersion(
            thresholds={"MONTHS_UNTIL_EMPTY": 6, "TINY_BALANCE": 100}
        ),
    )
    monkeypatch.setattr(
        watchlist,
        "get_settings",
        lambda: SimpleNamespace(agent_new_client_days=14, agent_awaiting_call_days=5),
    )

    thresholds = watchlist.load_thresholds(None, AS_OF)

    assert thresholds == WatchlistThresholds(
        new_client_days=14,
        months_until_empty=6.0,
        small_balance=100.0,
        awaiting_call_days=5,
    )


def test_load_thresholds_refuses_to_guess_without_a_risk_config(monkeypatch) -> None:
    monkeypatch.setattr(watchlist, "load_active_config", lambda session, at: None)

    with pytest.raises(WatchlistConfigMissing):
        watchlist.load_thresholds(None, AS_OF)
