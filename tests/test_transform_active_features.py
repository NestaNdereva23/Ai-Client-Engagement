"""Tests for deriving the active-book behavioural dimensions.

These are pure: they flatten a crafted active-clients payload and check the
derived measures, with no database involved.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.transform.active_features import (
    BALANCE_TIER_CUTOFFS,
    FEE_PER_MONTH,
    RECENCY_BAND_CUTOFFS,
    SYSTEM_FEE_MAX,
    VALUE_TIER_CUTOFFS,
    balance_tier,
    derive_active_measures,
    recency_band,
    value_tier,
)
from app.transform.active_flatten import flatten_active_payload

EAT = timezone(timedelta(hours=3))
ANCHOR = datetime(2026, 7, 23, 9, 0, tzinfo=EAT)


def _payload(
    deposits: list[tuple[int, str, str]],
    withdrawals: list[tuple[int, str, str]] | None = None,
    balance: float = 42_000.0,
) -> dict[str, Any]:
    """One client in one fund. Each txn tuple is (id, date, amount)."""
    return {
        "data": [
            {
                "unit_fund_id": 10,
                "unit_fund_name": "Money Market Fund",
                "client_count": 1,
                "clients": [
                    {
                        "client_id": 1001,
                        "balance": balance,
                        "last_5_purchases": [
                            {"id": i, "date": d, "number": a, "unit_fund_id": 10}
                            for (i, d, a) in deposits
                        ],
                        "last_2_sales": [
                            {"id": i, "date": d, "number": a, "unit_fund_id": 10}
                            for (i, d, a) in (withdrawals or [])
                        ],
                    }
                ],
            }
        ]
    }


def _only(payload: dict[str, Any], reference_date: datetime | None = ANCHOR):
    result = flatten_active_payload(payload, ANCHOR)
    measures = derive_active_measures(result, reference_date=reference_date)
    assert len(measures) == 1
    return next(iter(measures.values()))


def test_same_payload_gives_identical_output() -> None:
    payload = _payload(
        [
            (1, "2024-01-01T00:00:00", "100000"),
            (2, "2024-02-01T00:00:00", "50000"),
            (3, "2024-03-15T00:00:00", "10000"),
        ],
        withdrawals=[(50, "2024-04-01T00:00:00", "20000")],
    )
    first = _only(payload)
    second = _only(payload)
    assert first == second


def test_typical_gap_excludes_same_day_batch() -> None:
    # Two deposits booked the same day, then one a month later. The same-day
    # pair must not register as a zero-day pattern.
    deposits = [
        (1, "2024-01-01T00:00:00", "50000"),
        (2, "2024-01-01T00:00:00", "50000"),
        (3, "2024-02-01T00:00:00", "50000"),  # 31 days after the batch date
    ]
    m = _only(_payload(deposits))
    assert m.typical_gap_days == 31


def test_typical_gap_none_with_single_deposit_date() -> None:
    m = _only(_payload([(1, "2024-01-01T00:00:00", "50000")]))
    assert m.typical_gap_days is None


def test_deposit_cap_flags_capped() -> None:
    deposits = [(i, f"2024-0{i}-01T00:00:00", "10000") for i in range(1, 6)]  # 5 = the cap
    m = _only(_payload(deposits))
    assert m.deposit_count_capped is True


def test_both_withdrawal_slots_as_fee_postings_flags_history_hidden() -> None:
    fee_amount = str(SYSTEM_FEE_MAX - 1)
    withdrawals = [
        (50, "2024-01-01T00:00:00", fee_amount),
        (51, "2024-02-01T00:00:00", fee_amount),
    ]
    m = _only(_payload([(1, "2024-01-01T00:00:00", "10000")], withdrawals=withdrawals))
    assert m.withdrawal_history_hidden is True
    assert m.largest_withdrawal is None


def test_real_withdrawal_above_threshold_is_visible_despite_fee_posting() -> None:
    withdrawals = [
        (50, "2024-01-01T00:00:00", str(SYSTEM_FEE_MAX - 1)),  # fee posting
        (51, "2024-02-01T00:00:00", "15000"),  # a real withdrawal
    ]
    m = _only(_payload([(1, "2024-01-01T00:00:00", "10000")], withdrawals=withdrawals))
    assert m.largest_withdrawal == 15000.0
    # The withdrawal window is full, but a real withdrawal IS visible in it,
    # so this is capped history, not hidden.
    assert m.withdrawal_history_hidden is False


def test_one_fee_posting_alone_is_not_hidden() -> None:
    """A single fee-posting withdrawal doesn't fill the window
    (WITHDRAWAL_CAP is 2), so there's nothing to be hidden about yet --
    just no real withdrawal seen so far.
    """
    withdrawals = [(50, "2024-01-01T00:00:00", str(SYSTEM_FEE_MAX - 1))]
    m = _only(_payload([(1, "2024-01-01T00:00:00", "10000")], withdrawals=withdrawals))
    assert m.largest_withdrawal is None
    assert m.withdrawal_history_hidden is False


def test_deposit_trend_needs_three_points() -> None:
    two_points = [
        (1, "2024-01-01T00:00:00", "100000"),
        (2, "2024-02-01T00:00:00", "10000"),
    ]
    assert _only(_payload(two_points)).deposit_trend is None

    three_points = [*two_points, (3, "2024-03-01T00:00:00", "1000")]
    trend = _only(_payload(three_points)).deposit_trend
    assert trend is not None
    assert trend < 0  # each deposit smaller than the last


def test_last_deposit_amount_is_the_most_recent_deposit() -> None:
    deposits = [
        (1, "2024-01-01T00:00:00", "100000"),
        (2, "2024-03-01T00:00:00", "5000"),  # most recent by date
    ]
    assert _only(_payload(deposits)).last_deposit_amount == 5000.0


def test_withdrawal_pct_uses_balance_before_withdrawal() -> None:
    # balance now is 200,000 after a real withdrawal of 50,000, so the
    # balance before the withdrawal is 250,000 and withdrawal_pct is
    # 50,000 / 250,000 = 0.2.
    m = _only(
        _payload(
            [(1, "2024-01-01T00:00:00", "10000")],
            withdrawals=[(50, "2024-02-01T00:00:00", "50000")],
            balance=200_000.0,
        )
    )
    assert m.withdrawal_pct == 0.2


def test_last_withdrawal_date_is_the_most_recent_not_the_largest() -> None:
    # An older, larger real withdrawal and a more recent, smaller one: the
    # date tracks the most recent real withdrawal, independent of
    # largest_withdrawal.
    m = _only(
        _payload(
            [(1, "2024-01-01T00:00:00", "10000")],
            withdrawals=[
                (50, "2024-02-01T00:00:00", "80000"),
                (51, "2024-06-01T00:00:00", "20000"),
            ],
            balance=200_000.0,
        )
    )
    assert m.largest_withdrawal == 80000.0
    assert m.last_withdrawal_date == date(2024, 6, 1)


def test_last_withdrawal_date_ignores_system_fee_postings() -> None:
    m = _only(
        _payload(
            [(1, "2024-01-01T00:00:00", "10000")],
            withdrawals=[(50, "2024-06-01T00:00:00", str(SYSTEM_FEE_MAX))],
        )
    )
    assert m.largest_withdrawal is None
    assert m.last_withdrawal_date is None


def test_months_until_empty_uses_the_fixed_deduction_rate_not_observed_withdrawals() -> None:
    """months_until_empty is balance / FEE_PER_MONTH, a fixed rate -- not an
    average of whatever fee postings happen to be visible for this client.
    """
    m = _only(
        _payload(
            [(1, "2024-01-01T00:00:00", "10000")],
            withdrawals=[(50, "2024-02-01T00:00:00", "1")],  # a fee posting of a different size
            balance=FEE_PER_MONTH * 10,
        )
    )
    assert m.months_until_empty == 10.0


def test_months_until_empty_ignores_a_client_with_no_visible_withdrawals_at_all() -> None:
    m = _only(_payload([(1, "2024-01-01T00:00:00", "10000")], balance=FEE_PER_MONTH * 6))
    assert m.months_until_empty == 6.0


def test_recency_counts_days_since_deposit_not_any_transaction() -> None:
    m = _only(
        _payload(
            [(1, "2024-06-01T00:00:00", "10000")],
            withdrawals=[(50, "2026-07-20T00:00:00", "10000")],  # a much more recent withdrawal
        )
    )
    # Anchored 2026-07-23: days since the deposit, not the more recent withdrawal.
    assert m.days_since_deposit == (ANCHOR.date() - date(2024, 6, 1)).days


def test_recency_none_without_a_reference_date() -> None:
    m = _only(_payload([(1, "2024-01-01T00:00:00", "10000")]), reference_date=None)
    assert m.days_since_deposit is None


# --- bucketing: balance_tier, recency_band, value_tier ---


def test_balance_tier_unknown_when_balance_missing() -> None:
    assert balance_tier(None) == "Unknown"


def test_balance_tier_boundaries() -> None:
    tiny_max, micro_max, small_max, core_max, premium_max = BALANCE_TIER_CUTOFFS
    assert balance_tier(tiny_max) == "Tiny"
    assert balance_tier(tiny_max + 1) == "Micro"
    assert balance_tier(micro_max) == "Micro"
    assert balance_tier(micro_max + 1) == "Small"
    assert balance_tier(small_max) == "Small"
    assert balance_tier(small_max + 1) == "Core"
    assert balance_tier(core_max) == "Core"
    assert balance_tier(core_max + 1) == "Premium"
    assert balance_tier(premium_max) == "Premium"
    assert balance_tier(premium_max + 1) == "Institutional"


def test_recency_band_unknown_when_days_missing() -> None:
    assert recency_band(None) == "Unknown"


def test_recency_band_boundaries() -> None:
    one_m, three_m, six_m, one_y, two_y = RECENCY_BAND_CUTOFFS
    assert recency_band(one_m) == "<=1m"
    assert recency_band(one_m + 1) == "1-3m"
    assert recency_band(three_m) == "1-3m"
    assert recency_band(three_m + 1) == "3-6m"
    assert recency_band(six_m) == "3-6m"
    assert recency_band(six_m + 1) == "6-12m"
    assert recency_band(one_y) == "6-12m"
    assert recency_band(one_y + 1) == "1-2y"
    assert recency_band(two_y) == "1-2y"
    assert recency_band(two_y + 1) == "2y+"


def test_value_tier_unknown_when_avg_deposit_amount_missing() -> None:
    assert value_tier(None) == "Unknown"


def test_value_tier_boundaries() -> None:
    low_max, medium_max, high_max = VALUE_TIER_CUTOFFS
    assert value_tier(low_max) == "Low"
    assert value_tier(low_max + 1) == "Medium"
    assert value_tier(medium_max) == "Medium"
    assert value_tier(medium_max + 1) == "High"
    assert value_tier(high_max) == "High"
    assert value_tier(high_max + 1) == "Top"
