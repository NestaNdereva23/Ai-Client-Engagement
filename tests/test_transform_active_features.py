"""Tests for deriving the active-book behavioural dimensions.

These are pure: they flatten a crafted active-clients payload and check the
derived measures, with no database involved.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.transform.active_features import FEE_PER_MONTH, SYSTEM_SALE_MAX, derive_active_measures
from app.transform.active_flatten import flatten_active_payload

EAT = timezone(timedelta(hours=3))
ANCHOR = datetime(2026, 7, 23, 9, 0, tzinfo=EAT)


def _payload(
    purchases: list[tuple[int, str, str]],
    sales: list[tuple[int, str, str]] | None = None,
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
                            for (i, d, a) in purchases
                        ],
                        "last_2_sales": [
                            {"id": i, "date": d, "number": a, "unit_fund_id": 10}
                            for (i, d, a) in (sales or [])
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
        sales=[(50, "2024-04-01T00:00:00", "20000")],
    )
    first = _only(payload)
    second = _only(payload)
    assert first == second


def test_rhythm_excludes_same_day_batch() -> None:
    # Two purchases booked the same day, then one a month later. The same-day
    # pair must not register as a zero-day cadence.
    purchases = [
        (1, "2024-01-01T00:00:00", "50000"),
        (2, "2024-01-01T00:00:00", "50000"),
        (3, "2024-02-01T00:00:00", "50000"),  # 31 days after the batch date
    ]
    m = _only(_payload(purchases))
    assert m.rhythm_days == 31


def test_rhythm_none_with_single_purchase_date() -> None:
    m = _only(_payload([(1, "2024-01-01T00:00:00", "50000")]))
    assert m.rhythm_days is None


def test_purchase_cap_flags_censored() -> None:
    purchases = [(i, f"2024-0{i}-01T00:00:00", "10000") for i in range(1, 6)]  # 5 = the cap
    m = _only(_payload(purchases))
    assert m.purchases_censored is True


def test_both_sale_slots_as_fee_postings_flags_redemption_blind() -> None:
    fee_amount = str(SYSTEM_SALE_MAX - 1)
    sales = [
        (50, "2024-01-01T00:00:00", fee_amount),
        (51, "2024-02-01T00:00:00", fee_amount),
    ]
    m = _only(_payload([(1, "2024-01-01T00:00:00", "10000")], sales=sales))
    assert m.redemption_history_blind is True
    assert m.largest_real_sale is None


def test_real_sale_above_threshold_is_visible_despite_fee_posting() -> None:
    sales = [
        (50, "2024-01-01T00:00:00", str(SYSTEM_SALE_MAX - 1)),  # fee posting
        (51, "2024-02-01T00:00:00", "15000"),  # a real withdrawal
    ]
    m = _only(_payload([(1, "2024-01-01T00:00:00", "10000")], sales=sales))
    assert m.largest_real_sale == 15000.0
    # The sale window is full, but a real redemption IS visible in it, so
    # this is truncated history, not a blind one.
    assert m.redemption_history_blind is False


def test_one_fee_posting_alone_is_not_blind() -> None:
    """A single fee-posting sale doesn't fill the window (SALE_CAP is 2), so
    there's nothing to be blind about yet -- just no real sale seen so far.
    """
    sales = [(50, "2024-01-01T00:00:00", str(SYSTEM_SALE_MAX - 1))]
    m = _only(_payload([(1, "2024-01-01T00:00:00", "10000")], sales=sales))
    assert m.largest_real_sale is None
    assert m.redemption_history_blind is False


def test_ticket_trend_needs_three_points() -> None:
    two_points = [
        (1, "2024-01-01T00:00:00", "100000"),
        (2, "2024-02-01T00:00:00", "10000"),
    ]
    assert _only(_payload(two_points)).ticket_trend is None

    three_points = [*two_points, (3, "2024-03-01T00:00:00", "1000")]
    trend = _only(_payload(three_points)).ticket_trend
    assert trend is not None
    assert trend < 0  # each ticket smaller than the last


def test_last_ticket_is_the_most_recent_purchase() -> None:
    purchases = [
        (1, "2024-01-01T00:00:00", "100000"),
        (2, "2024-03-01T00:00:00", "5000"),  # most recent by date
    ]
    assert _only(_payload(purchases)).last_ticket == 5000.0


def test_drawdown_ratio_uses_implied_prior_balance() -> None:
    # balance now is 200,000 after a real sale of 50,000, so the implied
    # prior balance is 250,000 and the drawdown ratio is 50,000 / 250,000 = 0.2.
    m = _only(
        _payload(
            [(1, "2024-01-01T00:00:00", "10000")],
            sales=[(50, "2024-02-01T00:00:00", "50000")],
            balance=200_000.0,
        )
    )
    assert m.drawdown_ratio == 0.2


def test_fee_runway_uses_the_fixed_deduction_rate_not_observed_sales() -> None:
    """fee_runway_months is balance / FEE_PER_MONTH, a fixed rate -- not an
    average of whatever fee postings happen to be visible for this client.
    """
    m = _only(
        _payload(
            [(1, "2024-01-01T00:00:00", "10000")],
            sales=[(50, "2024-02-01T00:00:00", "1")],  # a fee posting of a different size
            balance=FEE_PER_MONTH * 10,
        )
    )
    assert m.fee_runway_months == 10.0


def test_fee_runway_ignores_a_client_with_no_visible_sales_at_all() -> None:
    m = _only(_payload([(1, "2024-01-01T00:00:00", "10000")], balance=FEE_PER_MONTH * 6))
    assert m.fee_runway_months == 6.0


def test_recency_counts_days_since_purchase_not_any_transaction() -> None:
    m = _only(
        _payload(
            [(1, "2024-06-01T00:00:00", "10000")],
            sales=[(50, "2026-07-20T00:00:00", "10000")],  # a much more recent sale
        )
    )
    # Anchored 2026-07-23: days since the purchase, not the more recent sale.
    assert m.days_since_purchase == (ANCHOR.date() - date(2024, 6, 1)).days


def test_recency_none_without_a_reference_date() -> None:
    m = _only(_payload([(1, "2024-01-01T00:00:00", "10000")]), reference_date=None)
    assert m.days_since_purchase is None
