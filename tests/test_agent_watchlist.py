"""The nightly watch list: one test per group, plus the empty case.

Seeds the active book and the risk rows directly, then checks each filter
picks up exactly the client funds a hand written query would.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.agents.situations import NEW_CLIENT_SINGLE_DEPOSIT
from app.agents.watchlist import (
    GETTING_SMALLER,
    GROUP_NAMES,
    HEALTHY_ONE_FUND,
    MORE_URGENT_BUT_NOT_CALLED,
    SIGNED_UP_RECENTLY,
    VERY_SMALL_AND_QUIET,
    WAITING_ON_A_CALL,
    WatchlistConfigMissing,
    WatchlistThresholds,
    build_watchlist,
    fee_pressure_active_contributor,
    fee_pressure_gone_quiet,
    getting_smaller,
    healthy_one_fund,
    more_urgent_but_not_called,
    signed_up_recently,
    very_small_and_quiet,
    waiting_on_a_call,
)
from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.digest import DigestLine, DigestRun
from app.db.models.risk import ClientRiskFeatures, RiskConfigVersion, RiskRun, RiskSnapshot
from app.db.models.signals import ClientSituationState, SignalRun
from app.db.session import SessionLocal

FUND_ID = 9455
SECOND_FUND_ID = 9456
NEW_CLIENT = 945501
FEE_CLIENT = 945502
QUIET_CLIENT = 945503
SHRINKING_CLIENT = 945504
HEALTHY_CLIENT = 945505
TWO_FUND_CLIENT = 945506
UNCALLED_CLIENT = 945507
CALLED_BACK_CLIENT = 945508

CLIENT_IDS = (
    NEW_CLIENT,
    FEE_CLIENT,
    QUIET_CLIENT,
    SHRINKING_CLIENT,
    HEALTHY_CLIENT,
    TWO_FUND_CLIENT,
    UNCALLED_CLIENT,
    CALLED_BACK_CLIENT,
)

RISK_RUN_ID = "watchlist-test-run"
EARLIER_RUN_ID = "watchlist-test-earlier-run"
SIGNAL_RUN_ID = "watchlist-test-signal-run"
AS_OF = date(2026, 9, 7)
CONFIG_VERSION = 1
REVIEWER = "watchlist-test-reviewer"

THRESHOLDS = WatchlistThresholds(
    new_client_days=30,
    months_until_empty=6.0,
    small_balance=100.0,
    awaiting_call_days=2,
)


def _fund(client_id: int, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance=500_000.0,
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
        risk_band="Low",
        risk_reasons="no signal",
        fund_at_risk=0.0,
        config_version=CONFIG_VERSION,
    )
    row.update(overrides)
    return ClientRiskFeatures(**row)


def _digest_line(digest_run_id: int, client_id: int, **overrides) -> DigestLine:
    row = dict(
        digest_run_id=digest_run_id,
        group_key="watchlist-test-group",
        group_total=1,
        rank=1,
        client_id=client_id,
        unit_fund_id=FUND_ID,
        risk_score=80,
        risk_band="High",
        risk_reasons="deposits are going down",
        fund_at_risk=100_000.0,
        route="fa_call_priority",
        in_call_queue=True,
    )
    row.update(overrides)
    return DigestLine(**row)


def _purge(session) -> None:
    session.execute(
        delete(ActiveClientInteraction).where(ActiveClientInteraction.client_id.in_(CLIENT_IDS))
    )
    session.execute(delete(DigestLine).where(DigestLine.client_id.in_(CLIENT_IDS)))
    session.execute(delete(DigestRun).where(DigestRun.risk_run_id == RISK_RUN_ID))
    session.execute(delete(RiskSnapshot).where(RiskSnapshot.client_id.in_(CLIENT_IDS)))
    session.execute(delete(RiskRun).where(RiskRun.run_id.in_((RISK_RUN_ID, EARLIER_RUN_ID))))
    session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(CLIENT_IDS)))
    session.execute(
        delete(ClientSituationState).where(ClientSituationState.client_id.in_(CLIENT_IDS))
    )
    session.execute(delete(SignalRun).where(SignalRun.run_id == SIGNAL_RUN_ID))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(CLIENT_IDS)))
    session.commit()


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session)

        session.add_all(
            [
                _fund(NEW_CLIENT, n_deposits=1, first_deposit_date=AS_OF - timedelta(days=3)),
                _fund(FEE_CLIENT, balance=4_000.0, months_until_empty=2.5),
                _fund(QUIET_CLIENT, balance=40.0),
                _fund(SHRINKING_CLIENT),
                _fund(HEALTHY_CLIENT),
                _fund(TWO_FUND_CLIENT),
                ActiveClientFund(
                    client_id=TWO_FUND_CLIENT,
                    unit_fund_id=SECOND_FUND_ID,
                    balance=200_000.0,
                    n_deposits=4,
                    n_withdrawals=0,
                ),
                _fund(UNCALLED_CLIENT, balance=300_000.0),
                _fund(CALLED_BACK_CLIENT, balance=250_000.0),
            ]
        )
        session.add_all(
            [
                _risk(QUIET_CLIENT, sig_dormant=True, risk_band="Watch"),
                _risk(SHRINKING_CLIENT, sig_shrinking=True, risk_band="Watch"),
                _risk(HEALTHY_CLIENT, risk_band="None"),
                _risk(TWO_FUND_CLIENT, risk_band="None"),
                _risk(UNCALLED_CLIENT, risk_band="High"),
                _risk(CALLED_BACK_CLIENT, risk_band="High"),
            ]
        )
        session.add(RiskRun(run_id=RISK_RUN_ID, state="completed", config_version=CONFIG_VERSION))
        session.add(SignalRun(run_id=SIGNAL_RUN_ID, state="completed"))
        session.flush()
        session.add(
            ClientSituationState(
                client_id=NEW_CLIENT,
                unit_fund_id=FUND_ID,
                situation_code=NEW_CLIENT_SINGLE_DEPOSIT,
                is_active=True,
                signal_codes=["single_deposit", "first_deposit_recent"],
                since=AS_OF,
                run_id=SIGNAL_RUN_ID,
            )
        )

        old_digest = DigestRun(
            risk_run_id=RISK_RUN_ID,
            generated_at=datetime(2026, 9, 1, 6, 0),
        )
        session.add(old_digest)
        session.flush()
        session.add_all(
            [
                _digest_line(old_digest.digest_run_id, UNCALLED_CLIENT),
                _digest_line(old_digest.digest_run_id, CALLED_BACK_CLIENT, rank=2),
            ]
        )
        session.add(
            ActiveClientInteraction(
                client_id=CALLED_BACK_CLIENT,
                unit_fund_id=FUND_ID,
                type="call_logged",
                reviewer_id=REVIEWER,
                created_at=datetime(2026, 9, 2, 9, 0),
            )
        )
        session.commit()

    yield

    with SessionLocal() as session:
        _purge(session)


def _keys(group) -> set[tuple[int, int]]:
    return {(member.client_id, member.unit_fund_id) for member in group.members}


def _seeded(group) -> set[tuple[int, int]]:
    return {key for key in _keys(group) if key[0] in CLIENT_IDS}


def test_signed_up_recently_finds_a_client_with_the_situation_active(
    book: None, monkeypatch
) -> None:
    from app.agents import watchlist

    monkeypatch.setattr(
        watchlist, "get_settings", lambda: SimpleNamespace(signal_situation_source="situations")
    )
    with SessionLocal() as session:
        group = signed_up_recently(session, THRESHOLDS, AS_OF)
    assert (NEW_CLIENT, FUND_ID) in _keys(group)
    assert (FEE_CLIENT, FUND_ID) not in _keys(group)


def test_signed_up_recently_ignores_a_client_whose_situation_is_inactive(
    book: None, monkeypatch
) -> None:
    from app.agents import watchlist

    monkeypatch.setattr(
        watchlist, "get_settings", lambda: SimpleNamespace(signal_situation_source="situations")
    )
    with SessionLocal() as session:
        state = session.get(ClientSituationState, (NEW_CLIENT, FUND_ID, NEW_CLIENT_SINGLE_DEPOSIT))
        state.is_active = False
        session.commit()
        group = signed_up_recently(session, THRESHOLDS, AS_OF)
    assert (NEW_CLIENT, FUND_ID) not in _keys(group)


def test_signed_up_recently_runs_the_old_query_when_the_source_is_legacy(book: None) -> None:
    with SessionLocal() as session:
        group = signed_up_recently(session, THRESHOLDS, AS_OF)
    assert (NEW_CLIENT, FUND_ID) in _keys(group)
    assert (FEE_CLIENT, FUND_ID) not in _keys(group)


def test_fee_pressure_groups_have_no_legacy_fallback(book: None) -> None:
    """Neither fee pressure group reads the situation table under legacy, so
    with nothing recomputed today both come back empty rather than guessing.
    """
    with SessionLocal() as session:
        gone_quiet = fee_pressure_gone_quiet(session, THRESHOLDS, AS_OF)
        active_contributor = fee_pressure_active_contributor(session, THRESHOLDS, AS_OF)
    assert _seeded(gone_quiet) == set()
    assert _seeded(active_contributor) == set()


def _force_legacy(monkeypatch) -> None:
    from app.agents import watchlist

    monkeypatch.setattr(
        watchlist, "get_settings", lambda: SimpleNamespace(signal_situation_source="legacy")
    )


def test_very_small_and_quiet_needs_both_a_small_balance_and_no_movement(
    book: None, monkeypatch
) -> None:
    _force_legacy(monkeypatch)
    with SessionLocal() as session:
        group = very_small_and_quiet(session, THRESHOLDS, AS_OF)
    assert _seeded(group) == {(QUIET_CLIENT, FUND_ID)}


def test_very_small_and_quiet_skips_a_small_balance_that_still_moves(
    book: None, monkeypatch
) -> None:
    _force_legacy(monkeypatch)
    with SessionLocal() as session:
        row = session.get(ClientRiskFeatures, (QUIET_CLIENT, FUND_ID))
        row.sig_dormant = False
        session.commit()
        group = very_small_and_quiet(session, THRESHOLDS, AS_OF)
    assert _seeded(group) == set()


def test_getting_smaller_finds_the_falling_deposits_client(book: None, monkeypatch) -> None:
    _force_legacy(monkeypatch)
    with SessionLocal() as session:
        group = getting_smaller(session, THRESHOLDS, AS_OF)
    assert _seeded(group) == {(SHRINKING_CLIENT, FUND_ID)}


def test_healthy_one_fund_leaves_out_a_client_holding_two_funds(book: None, monkeypatch) -> None:
    _force_legacy(monkeypatch)
    with SessionLocal() as session:
        group = healthy_one_fund(session, THRESHOLDS, AS_OF)
    assert (HEALTHY_CLIENT, FUND_ID) in _keys(group)
    assert (TWO_FUND_CLIENT, FUND_ID) not in _keys(group)
    assert (TWO_FUND_CLIENT, SECOND_FUND_ID) not in _keys(group)


def test_waiting_on_a_call_finds_the_client_nobody_rang(book: None, monkeypatch) -> None:
    _force_legacy(monkeypatch)
    with SessionLocal() as session:
        group = waiting_on_a_call(session, THRESHOLDS, AS_OF)
    assert _seeded(group) == {(UNCALLED_CLIENT, FUND_ID)}


def test_waiting_on_a_call_skips_a_digest_that_is_still_fresh(book: None, monkeypatch) -> None:
    _force_legacy(monkeypatch)
    fresh = WatchlistThresholds(
        new_client_days=THRESHOLDS.new_client_days,
        months_until_empty=THRESHOLDS.months_until_empty,
        small_balance=THRESHOLDS.small_balance,
        awaiting_call_days=30,
    )
    with SessionLocal() as session:
        group = waiting_on_a_call(session, fresh, AS_OF)
    assert _seeded(group) == set()


def test_a_group_counts_clients_funds_and_money(book: None) -> None:
    with SessionLocal() as session:
        group = signed_up_recently(session, THRESHOLDS, AS_OF)
    assert group.client_count == 1
    assert group.fund_count == 1
    assert group.money_total == 500_000.0


def test_a_group_carries_no_client_name(book: None) -> None:
    with SessionLocal() as session:
        group = signed_up_recently(session, THRESHOLDS, AS_OF)
    member_fields = set(vars(group.members[0]))
    assert member_fields == {"client_id", "unit_fund_id", "balance"}


def test_build_watchlist_returns_every_group_in_order(book: None) -> None:
    with SessionLocal() as session:
        groups = build_watchlist(session, AS_OF, THRESHOLDS)
    assert tuple(group.name for group in groups) == GROUP_NAMES


def test_a_group_with_nobody_in_it_is_still_returned(book: None) -> None:
    with SessionLocal() as session:
        session.execute(
            delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id == SHRINKING_CLIENT)
        )
        session.commit()
        groups = build_watchlist(session, AS_OF, THRESHOLDS)
    by_name = {group.name: group for group in groups}
    assert set(by_name) == set(GROUP_NAMES)
    assert _seeded(by_name[GETTING_SMALLER]) == set()
    assert by_name[GETTING_SMALLER].client_count >= 0


def test_every_named_group_has_a_written_definition(book: None, monkeypatch) -> None:
    _force_legacy(monkeypatch)
    with SessionLocal() as session:
        groups = build_watchlist(session, AS_OF, THRESHOLDS)
    for group in groups:
        assert group.definition
    by_name = {group.name: group for group in groups}
    assert by_name[SIGNED_UP_RECENTLY].definition["situation_code"] == NEW_CLIENT_SINGLE_DEPOSIT
    assert by_name[VERY_SMALL_AND_QUIET].definition["balance_below"] == 100.0
    assert by_name[HEALTHY_ONE_FUND].definition["funds_held"] == 1
    assert by_name[WAITING_ON_A_CALL].definition["in_call_queue"] is True


def _snapshot(run_id: str, client_id: int, route: str) -> RiskSnapshot:
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


@pytest.fixture
def two_nights(book: None):
    """Last night and the night before, for the same four client funds.

    QUIET_CLIENT moved up but missed the call list, SHRINKING_CLIENT moved
    up straight onto it, HEALTHY_CLIENT moved down, TWO_FUND_CLIENT did not
    move at all.
    """
    with SessionLocal() as session:
        session.add(
            RiskRun(
                run_id=EARLIER_RUN_ID,
                state="completed",
                config_version=CONFIG_VERSION,
                finished_at=datetime(2026, 9, 5, 2, 0),
            )
        )
        session.flush()
        session.add_all(
            [
                _snapshot(EARLIER_RUN_ID, QUIET_CLIENT, "monitor_only"),
                _snapshot(EARLIER_RUN_ID, SHRINKING_CLIENT, "auto_checkin"),
                _snapshot(EARLIER_RUN_ID, HEALTHY_CLIENT, "fa_watchlist"),
                _snapshot(EARLIER_RUN_ID, TWO_FUND_CLIENT, "monitor_only"),
            ]
        )
        session.flush()
        session.add_all(
            [
                _snapshot(RISK_RUN_ID, QUIET_CLIENT, "fa_watchlist"),
                _snapshot(RISK_RUN_ID, SHRINKING_CLIENT, "fa_call_priority"),
                _snapshot(RISK_RUN_ID, HEALTHY_CLIENT, "monitor_only"),
                _snapshot(RISK_RUN_ID, TWO_FUND_CLIENT, "monitor_only"),
                _snapshot(RISK_RUN_ID, UNCALLED_CLIENT, "fa_watchlist"),
            ]
        )
        session.commit()
    yield


def test_more_urgent_but_not_called_finds_the_client_who_missed_the_list(
    two_nights: None, monkeypatch
) -> None:
    _force_legacy(monkeypatch)
    with SessionLocal() as session:
        group = more_urgent_but_not_called(session, THRESHOLDS, AS_OF, run_id=RISK_RUN_ID)
    assert _seeded(group) == {(QUIET_CLIENT, FUND_ID)}


def test_more_urgent_but_not_called_leaves_out_a_client_on_the_call_list(
    two_nights: None, monkeypatch
) -> None:
    _force_legacy(monkeypatch)
    with SessionLocal() as session:
        group = more_urgent_but_not_called(session, THRESHOLDS, AS_OF, run_id=RISK_RUN_ID)
    assert (SHRINKING_CLIENT, FUND_ID) not in _keys(group)


def test_more_urgent_but_not_called_leaves_out_a_client_who_moved_down(
    two_nights: None, monkeypatch
) -> None:
    _force_legacy(monkeypatch)
    with SessionLocal() as session:
        group = more_urgent_but_not_called(session, THRESHOLDS, AS_OF, run_id=RISK_RUN_ID)
    assert (HEALTHY_CLIENT, FUND_ID) not in _keys(group)


def test_more_urgent_but_not_called_leaves_out_a_client_who_did_not_move(
    two_nights: None, monkeypatch
) -> None:
    _force_legacy(monkeypatch)
    with SessionLocal() as session:
        group = more_urgent_but_not_called(session, THRESHOLDS, AS_OF, run_id=RISK_RUN_ID)
    assert (TWO_FUND_CLIENT, FUND_ID) not in _keys(group)


def test_more_urgent_but_not_called_leaves_out_a_first_ever_run(
    two_nights: None, monkeypatch
) -> None:
    _force_legacy(monkeypatch)
    with SessionLocal() as session:
        group = more_urgent_but_not_called(session, THRESHOLDS, AS_OF, run_id=RISK_RUN_ID)
    assert (UNCALLED_CLIENT, FUND_ID) not in _keys(group)


def test_more_urgent_but_not_called_is_empty_with_no_finished_run(monkeypatch) -> None:
    from app.agents import watchlist

    _force_legacy(monkeypatch)
    monkeypatch.setattr(watchlist, "latest_completed_run_id", lambda session: None)
    group = watchlist.more_urgent_but_not_called(None, THRESHOLDS, AS_OF)

    assert group.name == MORE_URGENT_BUT_NOT_CALLED
    assert group.members == ()
    assert group.money_total == 0
    assert group.definition["on_the_call_list"] is False


def test_load_thresholds_reads_settings_and_the_risk_config(monkeypatch) -> None:
    from app.agents import watchlist

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
    from app.agents import watchlist

    monkeypatch.setattr(watchlist, "load_active_config", lambda session, at: None)

    with pytest.raises(WatchlistConfigMissing):
        watchlist.load_thresholds(None, AS_OF)
