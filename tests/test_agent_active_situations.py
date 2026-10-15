from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update

from app.agents.signals import (
    _open_run,
    recompute_near_preservation_line_signal,
    recompute_net_flow_negative_signal,
    recompute_past_typical_gap_signal,
    recompute_side_changed_downward_signal,
    recompute_single_fund_held_signal,
    recompute_tier_signals,
)
from app.agents.situations import (
    APPROACHING_PRESERVATION_LINE,
    CROSSED_BELOW_LINE,
    FREQUENT_DEPOSITOR_ONE_FUND,
    FREQUENT_DEPOSITOR_WITHDRAWING,
    GRADUAL_DEPOSITOR_DUE,
    GRADUAL_DEPOSITOR_WITHDRAWING,
    SITUATION_CODES,
    recompute_approaching_preservation_line,
    recompute_crossed_below_line,
    recompute_frequent_depositor_one_fund,
    recompute_frequent_depositor_withdrawing,
    recompute_gradual_depositor_due,
    recompute_gradual_depositor_withdrawing,
)
from app.db.models.active_clients import ActiveClientFund, ActiveTransaction
from app.db.models.routing import ClientSide, ClientTier
from app.db.models.signals import (
    ClientSignalSnapshot,
    ClientSignalState,
    ClientSituationSnapshot,
    ClientSituationState,
)
from app.db.session import SessionLocal

AS_OF = date(2026, 10, 13)
FUND_A = 98061
FUND_B = 98062
FIRST_CLIENT = 980601
CLIENT_IDS = tuple(range(FIRST_CLIENT, FIRST_CLIENT + 20))
TIER_SITUATIONS = (
    GRADUAL_DEPOSITOR_DUE,
    GRADUAL_DEPOSITOR_WITHDRAWING,
    FREQUENT_DEPOSITOR_ONE_FUND,
    FREQUENT_DEPOSITOR_WITHDRAWING,
)

_next_txn_id = iter(range(98060001, 98070000))

DUE = FIRST_CLIENT
INSIDE_OWN_GAP = FIRST_CLIENT + 1
NO_GAP_YET = FIRST_CLIENT + 2
GRADUAL_WITHDRAWING = FIRST_CLIENT + 3
ONE_FUND = FIRST_CLIENT + 4
TWO_FUNDS = FIRST_CLIENT + 5
ONE_FUND_WITHDRAWER = FIRST_CLIENT + 6
FREQUENT_WITHDRAWING = FIRST_CLIENT + 7
FREQUENT_NET_POSITIVE = FIRST_CLIENT + 8
HELD_OUT = FIRST_CLIENT + 9
NEAR_LINE = FIRST_CLIENT + 10
DROPPED_BELOW_LINE = FIRST_CLIENT + 11
SAFE_ABOVE_LINE = FIRST_CLIENT + 12


def _fund(client_id: int, unit_fund_id: int = FUND_A, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        balance=100_000.0,
        n_deposits=3,
        n_withdrawals=0,
    )
    row.update(overrides)
    return ActiveClientFund(**row)


def _tier(client_id: int, tier: str, segment: str | None, **overrides) -> ClientTier:
    row = dict(
        client_id=client_id,
        purchased_recently=False,
        real_withdrawer=False,
        tier=tier,
        segment=segment,
        computed_at=datetime(2026, 10, 13, 6, 0, tzinfo=UTC),
        thresholds_version=1,
    )
    row.update(overrides)
    return ClientTier(**row)


def _side(client_id: int, side: str, previous_side: str | None) -> ClientSide:
    return ClientSide(
        client_id=client_id,
        side=side,
        previous_side=previous_side,
        balance=1_000.0,
        threshold_kes=6_000.0,
        source_feed="active",
    )


def _txn(client_id: int, kind: str, amount: float) -> ActiveTransaction:
    return ActiveTransaction(
        txn_id=next(_next_txn_id),
        txn_type=kind,
        client_id=client_id,
        unit_fund_id=FUND_A,
        txn_date=AS_OF - timedelta(days=30),
        amount=amount,
    )


def _purge(session) -> None:
    for model in (
        ClientSituationSnapshot,
        ClientSituationState,
        ClientSignalSnapshot,
        ClientSignalState,
        ActiveClientFund,
        ActiveTransaction,
        ClientTier,
        ClientSide,
    ):
        session.execute(delete(model).where(model.client_id.in_(CLIENT_IDS)))
    session.commit()


def _long_ago(days: int = 120) -> date:
    return AS_OF - timedelta(days=days)


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session)
        gradual = ("gradual_depositor", "active_dormant")
        gradual_w = ("gradual_depositor_withdrawer", "active_dormant")
        frequent = ("frequent_depositor", "active_active")
        frequent_w = ("frequent_depositor_withdrawer", "active_active")
        session.add_all(
            [
                _fund(DUE, last_deposit_date=_long_ago(), typical_gap_days=60),
                _tier(DUE, *gradual),
                _fund(INSIDE_OWN_GAP, last_deposit_date=_long_ago(), typical_gap_days=200),
                _tier(INSIDE_OWN_GAP, *gradual),
                _fund(NO_GAP_YET, last_deposit_date=_long_ago(), typical_gap_days=None),
                _tier(NO_GAP_YET, *gradual),
                _fund(GRADUAL_WITHDRAWING, last_deposit_date=_long_ago(), typical_gap_days=60),
                _tier(GRADUAL_WITHDRAWING, *gradual_w, real_withdrawer=True),
                _fund(ONE_FUND, last_deposit_date=_long_ago(10)),
                _tier(ONE_FUND, *frequent, purchased_recently=True),
                _fund(TWO_FUNDS, FUND_A, last_deposit_date=_long_ago(10)),
                _fund(TWO_FUNDS, FUND_B, last_deposit_date=_long_ago(10)),
                _tier(TWO_FUNDS, *frequent, purchased_recently=True),
                _fund(ONE_FUND_WITHDRAWER, last_deposit_date=_long_ago(10)),
                _tier(
                    ONE_FUND_WITHDRAWER, *frequent_w, purchased_recently=True, real_withdrawer=True
                ),
                _fund(FREQUENT_WITHDRAWING, last_deposit_date=_long_ago(10)),
                _tier(
                    FREQUENT_WITHDRAWING, *frequent_w, purchased_recently=True, real_withdrawer=True
                ),
                _txn(FREQUENT_WITHDRAWING, "purchase", 1_000),
                _txn(FREQUENT_WITHDRAWING, "sale", 2_000),
                _fund(FREQUENT_NET_POSITIVE, last_deposit_date=_long_ago(10)),
                _tier(
                    FREQUENT_NET_POSITIVE,
                    *frequent_w,
                    purchased_recently=True,
                    real_withdrawer=True,
                ),
                _txn(FREQUENT_NET_POSITIVE, "purchase", 5_000),
                _txn(FREQUENT_NET_POSITIVE, "sale", 1_000),
                _fund(HELD_OUT, last_deposit_date=_long_ago(), typical_gap_days=60, balance=500.0),
                _tier(HELD_OUT, "below_capital_preservation_threshold", None),
                _fund(NEAR_LINE, balance=6_100.0),
                _tier(NEAR_LINE, *gradual),
                _side(NEAR_LINE, "active", None),
                _fund(DROPPED_BELOW_LINE, balance=3_000.0),
                _side(DROPPED_BELOW_LINE, "inactive", "active"),
                _fund(SAFE_ABOVE_LINE, balance=50_000.0),
                _side(SAFE_ABOVE_LINE, "active", None),
            ]
        )
        session.commit()
    yield
    with SessionLocal() as session:
        _purge(session)


def _run_signals_and_situations(session) -> None:
    run = _open_run(session)
    recompute_tier_signals(session, AS_OF, run)
    recompute_past_typical_gap_signal(session, AS_OF, run)
    recompute_net_flow_negative_signal(session, AS_OF, run)
    recompute_single_fund_held_signal(session, AS_OF, run)
    recompute_near_preservation_line_signal(session, AS_OF, run)
    recompute_side_changed_downward_signal(session, AS_OF, run)
    session.commit()
    recompute_gradual_depositor_due(session, AS_OF, run.run_id)
    recompute_gradual_depositor_withdrawing(session, AS_OF, run.run_id)
    recompute_frequent_depositor_one_fund(session, AS_OF, run.run_id)
    recompute_frequent_depositor_withdrawing(session, AS_OF, run.run_id)
    recompute_approaching_preservation_line(session, AS_OF, run.run_id)
    recompute_crossed_below_line(session, AS_OF, run.run_id)


def _active_codes(session, client_id: int) -> set[str]:
    return set(
        session.scalars(
            select(ClientSituationState.situation_code).where(
                ClientSituationState.client_id == client_id,
                ClientSituationState.is_active.is_(True),
            )
        )
    )


@pytest.mark.parametrize(
    ("client_id", "expected"),
    [
        (DUE, {GRADUAL_DEPOSITOR_DUE}),
        (INSIDE_OWN_GAP, set()),
        (NO_GAP_YET, set()),
        (GRADUAL_WITHDRAWING, {GRADUAL_DEPOSITOR_WITHDRAWING}),
        (ONE_FUND, {FREQUENT_DEPOSITOR_ONE_FUND}),
        (TWO_FUNDS, set()),
        (ONE_FUND_WITHDRAWER, set()),
        (FREQUENT_WITHDRAWING, {FREQUENT_DEPOSITOR_WITHDRAWING}),
        (FREQUENT_NET_POSITIVE, set()),
        (HELD_OUT, set()),
        (NEAR_LINE, {APPROACHING_PRESERVATION_LINE}),
        (DROPPED_BELOW_LINE, {CROSSED_BELOW_LINE}),
        (SAFE_ABOVE_LINE, set()),
    ],
)
def test_each_tier_situation_fires_for_its_client_and_stays_off_for_the_near_miss(
    book, client_id, expected
) -> None:
    with SessionLocal() as session:
        _run_signals_and_situations(session)
        assert _active_codes(session, client_id) == expected


def test_a_gradual_depositor_who_withdraws_is_not_also_due(book) -> None:
    with SessionLocal() as session:
        _run_signals_and_situations(session)
        codes = _active_codes(session, GRADUAL_WITHDRAWING)
    assert GRADUAL_DEPOSITOR_DUE not in codes


def test_no_client_is_in_more_than_one_tier_situation(book) -> None:
    with SessionLocal() as session:
        _run_signals_and_situations(session)
        for client_id in CLIENT_IDS:
            assert len(_active_codes(session, client_id) & set(TIER_SITUATIONS)) <= 1


def test_a_client_who_drops_below_the_line_leaves_the_approaching_group_next_run(book) -> None:
    with SessionLocal() as session:
        _run_signals_and_situations(session)
        assert _active_codes(session, NEAR_LINE) == {APPROACHING_PRESERVATION_LINE}

        session.execute(
            update(ActiveClientFund)
            .where(ActiveClientFund.client_id == NEAR_LINE)
            .values(balance=3_000.0)
        )
        session.execute(
            update(ClientTier)
            .where(ClientTier.client_id == NEAR_LINE)
            .values(tier="below_capital_preservation_threshold", segment=None)
        )
        session.execute(
            update(ClientSide)
            .where(ClientSide.client_id == NEAR_LINE)
            .values(side="inactive", previous_side="active")
        )
        session.commit()

        _run_signals_and_situations(session)
        assert _active_codes(session, NEAR_LINE) == {CROSSED_BELOW_LINE}


def test_the_tier_and_line_situations_are_registered() -> None:
    assert {*TIER_SITUATIONS, APPROACHING_PRESERVATION_LINE, CROSSED_BELOW_LINE} <= set(
        SITUATION_CODES
    )
