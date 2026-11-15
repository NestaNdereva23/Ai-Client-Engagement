from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.agents.signals import (
    NEAR_PRESERVATION_LINE,
    NET_FLOW_NEGATIVE,
    NO_RECENT_PURCHASE,
    PAST_TYPICAL_GAP,
    PURCHASED_THIS_PERIOD,
    REAL_WITHDRAWER,
    SEGMENT_MOVED_UP,
    SIDE_CHANGED_DOWNWARD,
    SIGNAL_CODES,
    recompute_all_signals,
    recompute_near_preservation_line_signal,
    recompute_net_flow_negative_signal,
    recompute_past_typical_gap_signal,
    recompute_segment_moved_up_signal,
    recompute_side_changed_downward_signal,
    recompute_tier_signals,
)
from app.db.models.active_clients import ActiveClientFund, ActiveTransaction
from app.db.models.routing import ClientSide, ClientTier
from app.db.models.signals import ClientSignalSnapshot, ClientSignalState
from app.db.session import SessionLocal

AS_OF = date(2026, 10, 13)
FUND_A = 98051
FUND_B = 98052
FIRST_CLIENT = 980501
CLIENT_IDS = tuple(range(FIRST_CLIENT, FIRST_CLIENT + 40))

_next_txn_id = iter(range(98050001, 98060000))


def _fund(client_id: int, unit_fund_id: int = FUND_A, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        balance=100_000.0,
        n_deposits=2,
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


def _txn(client_id: int, kind: str, amount: float, days_ago: int) -> ActiveTransaction:
    return ActiveTransaction(
        txn_id=next(_next_txn_id),
        txn_type=kind,
        client_id=client_id,
        unit_fund_id=FUND_A,
        txn_date=AS_OF - timedelta(days=days_ago),
        amount=amount,
    )


def _purge(session) -> None:
    for model in (ClientSignalSnapshot, ClientSignalState, ActiveClientFund, ActiveTransaction):
        session.execute(delete(model).where(model.client_id.in_(CLIENT_IDS)))
    for model in (ClientTier, ClientSide):
        session.execute(delete(model).where(model.client_id.in_(CLIENT_IDS)))
    session.commit()


@pytest.fixture
def clean(db: None):
    with SessionLocal() as session:
        _purge(session)
    yield
    with SessionLocal() as session:
        _purge(session)


def _is_active(session, client_id: int, signal_code: str, unit_fund_id: int = FUND_A) -> bool:
    return session.get(ClientSignalState, (client_id, unit_fund_id, signal_code)).is_active


def test_tier_signals_follow_the_two_facts_and_a_held_out_client_gets_none(clean) -> None:
    frequent, gradual_withdrawer, held_out = FIRST_CLIENT, FIRST_CLIENT + 1, FIRST_CLIENT + 2
    with SessionLocal() as session:
        session.add_all(
            [
                _fund(frequent),
                _fund(gradual_withdrawer),
                _fund(held_out),
                _tier(frequent, "frequent_depositor", "active_active", purchased_recently=True),
                _tier(
                    gradual_withdrawer,
                    "gradual_depositor_withdrawer",
                    "active_dormant",
                    real_withdrawer=True,
                ),
                _tier(held_out, "no_transactions_in_review_period", None),
            ]
        )
        session.commit()
        recompute_tier_signals(session, AS_OF)
        seen = {
            client_id: [
                _is_active(session, client_id, code)
                for code in (NO_RECENT_PURCHASE, PURCHASED_THIS_PERIOD, REAL_WITHDRAWER)
            ]
            for client_id in (frequent, gradual_withdrawer, held_out)
        }
    assert seen[frequent] == [False, True, False]
    assert seen[gradual_withdrawer] == [True, False, True]
    assert seen[held_out] == [False, False, False]


def test_past_typical_gap_uses_the_fund_with_the_newest_deposit(clean) -> None:
    late, on_time, no_gap, two_funds = (FIRST_CLIENT + n for n in range(4))
    with SessionLocal() as session:
        session.add_all(
            [
                _fund(late, last_deposit_date=AS_OF - timedelta(days=100), typical_gap_days=60),
                _fund(on_time, last_deposit_date=AS_OF - timedelta(days=60), typical_gap_days=60),
                _fund(no_gap, last_deposit_date=AS_OF - timedelta(days=100), typical_gap_days=None),
                _fund(
                    two_funds,
                    FUND_A,
                    last_deposit_date=AS_OF - timedelta(days=200),
                    typical_gap_days=30,
                ),
                _fund(
                    two_funds,
                    FUND_B,
                    last_deposit_date=AS_OF - timedelta(days=10),
                    typical_gap_days=60,
                ),
            ]
        )
        session.commit()
        recompute_past_typical_gap_signal(session, AS_OF)
        seen = [
            _is_active(session, client_id, PAST_TYPICAL_GAP)
            for client_id in (late, on_time, no_gap, two_funds)
        ]
    assert seen == [True, False, False, False]


def test_net_flow_negative_needs_real_withdrawals_at_least_as_big_as_deposits(clean) -> None:
    cases = {
        "out_more": ([("purchase", 1_000, 30), ("sale", 2_000, 30)], True),
        "equal": ([("purchase", 1_000, 30), ("sale", 1_000, 30)], True),
        "in_more": ([("purchase", 2_000, 30), ("sale", 1_000, 30)], False),
        "only_fees": ([("sale", 50, 10), ("sale", 50, 40)], False),
        "nothing": ([], False),
        "outside_period": ([("purchase", 100, 30), ("sale", 5_000, 400)], False),
        "just_over_the_fee": ([("sale", 51, 30)], True),
    }
    clients = {name: FIRST_CLIENT + index for index, name in enumerate(cases)}
    with SessionLocal() as session:
        session.add_all(_fund(client_id) for client_id in clients.values())
        for name, (transactions, _) in cases.items():
            session.add_all(_txn(clients[name], *txn) for txn in transactions)
        session.commit()
        recompute_net_flow_negative_signal(session, AS_OF)
        seen = {name: _is_active(session, clients[name], NET_FLOW_NEGATIVE) for name in cases}
    assert seen == {name: expected for name, (_, expected) in cases.items()}


def test_near_preservation_line_projects_three_months_of_fees_and_net_outflow(clean) -> None:
    near_fees, on_the_edge, safe, draining, below, two_funds = (FIRST_CLIENT + n for n in range(6))
    with SessionLocal() as session:
        session.add_all(
            [
                _fund(near_fees, balance=6_100.0),
                _fund(on_the_edge, balance=6_150.0),
                _fund(safe, balance=6_200.0),
                _fund(draining, balance=20_000.0),
                _fund(below, balance=5_000.0),
                _fund(two_funds, FUND_A, balance=3_050.0),
                _fund(two_funds, FUND_B, balance=3_050.0),
                _txn(draining, "sale", 120_000, 100),
            ]
        )
        session.commit()
        recompute_near_preservation_line_signal(session, AS_OF)
        seen = [
            _is_active(session, client_id, NEAR_PRESERVATION_LINE)
            for client_id in (near_fees, on_the_edge, safe, draining, below, two_funds)
        ]
    assert seen == [True, False, False, True, False, True]


def test_side_changed_downward_stays_on_while_the_client_is_below_the_line(clean) -> None:
    dropped, always_inactive, climbed_back = FIRST_CLIENT, FIRST_CLIENT + 1, FIRST_CLIENT + 2
    sides = {
        dropped: ("inactive", "active"),
        always_inactive: ("inactive", None),
        climbed_back: ("active", "inactive"),
    }
    with SessionLocal() as session:
        for client_id, (side, previous_side) in sides.items():
            session.add(_fund(client_id))
            session.add(
                ClientSide(
                    client_id=client_id,
                    side=side,
                    previous_side=previous_side,
                    balance=1_000.0,
                    threshold_kes=6_000.0,
                    source_feed="active",
                )
            )
        session.commit()
        recompute_side_changed_downward_signal(session, AS_OF)
        seen = [
            _is_active(session, client_id, SIDE_CHANGED_DOWNWARD)
            for client_id in (dropped, always_inactive, climbed_back)
        ]
    assert seen == [True, False, False]


def test_segment_moved_up_needs_a_recent_move_from_active_dormant(clean) -> None:
    changed_at = {"recent": AS_OF - timedelta(days=3), "old": AS_OF - timedelta(days=10)}
    cases = {
        "moved_up": ("frequent_depositor", "gradual_depositor", "recent", True),
        "withdrawer_moved_up": (
            "frequent_depositor_withdrawer",
            "gradual_depositor_withdrawer",
            "recent",
            True,
        ),
        "moved_up_long_ago": ("frequent_depositor", "gradual_depositor", "old", False),
        "first_run": ("frequent_depositor", None, "recent", False),
        "went_down": ("gradual_depositor", "frequent_depositor", "recent", False),
        "came_from_held_out": (
            "frequent_depositor",
            "no_transactions_in_review_period",
            "recent",
            False,
        ),
    }
    clients = {name: FIRST_CLIENT + index for index, name in enumerate(cases)}
    with SessionLocal() as session:
        for name, (tier, previous_tier, when, _) in cases.items():
            segment = "active_active" if tier.startswith("frequent") else "active_dormant"
            session.add(_fund(clients[name]))
            session.add(
                _tier(
                    clients[name],
                    tier,
                    segment,
                    previous_tier=previous_tier,
                    tier_changed_at=(
                        None
                        if previous_tier is None
                        else datetime.combine(changed_at[when], datetime.min.time(), tzinfo=UTC)
                    ),
                )
            )
        session.commit()
        recompute_segment_moved_up_signal(session, AS_OF)
        seen = {name: _is_active(session, clients[name], SEGMENT_MOVED_UP) for name in cases}
    assert seen == {name: expected for name, (*_, expected) in cases.items()}


def test_the_full_signal_pass_writes_every_signal_code(clean) -> None:
    client_id = FIRST_CLIENT
    with SessionLocal() as session:
        session.add_all([_fund(client_id), _tier(client_id, "gradual_depositor", "active_dormant")])
        session.commit()
        run = recompute_all_signals(
            session,
            AS_OF,
            months_until_empty_threshold=12,
            small_balance_threshold=100,
            awaiting_call_days=2,
        )
        written = {
            code
            for code in SIGNAL_CODES
            if session.get(ClientSignalState, (client_id, FUND_A, code)) is not None
        }
    assert run.state == "completed"
    assert written == set(SIGNAL_CODES)
