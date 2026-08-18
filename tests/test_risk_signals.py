"""Tests for the six risk signals against constructed edge-case rows."""

from __future__ import annotations

from app.risk.signals import (
    SIGNAL_FUNCS,
    SIGNAL_LABELS,
    fired_signals,
    sig_broken_pattern,
    sig_dormant,
    sig_going_dormant,
    sig_heavy_withdrawal,
    sig_never_repeated,
    sig_shrinking,
)
from app.transform.active_features import ActiveFeatureMeasures

THRESHOLDS = {
    "DORMANT_DAYS": 365,
    "HEAVY_WITHDRAWAL_PCT": 0.50,
    "OVERDUE_MULTIPLE": 3.0,
    "SHRINKING_TREND": -0.10,
    "TINY_BALANCE": 100,
    "WORTH_A_CALL_BALANCE": 10_000,
    "MONTHS_UNTIL_EMPTY": 12,
    "FEE_PER_MONTH": 50,
    "SYSTEM_FEE_MAX": 100,
}


def _row(**overrides) -> ActiveFeatureMeasures:
    base = dict(
        client_id=1,
        unit_fund_id=10,
        balance=500_000.0,
        n_deposits=3,
        typical_gap_days=30.0,
        avg_deposit_amount=50_000.0,
        max_deposit_amount=100_000.0,
        last_deposit_amount=50_000.0,
        deposit_trend=0.0,
        largest_withdrawal=None,
        last_withdrawal_date=None,
        withdrawal_pct=None,
        months_until_empty=None,
        days_since_deposit=10,
        deposit_count_capped=False,
        withdrawal_history_hidden=False,
    )
    base.update(overrides)
    return ActiveFeatureMeasures(**base)


# --- sig_heavy_withdrawal ---


def test_heavy_withdrawal_fires_at_or_above_the_heavy_threshold() -> None:
    assert sig_heavy_withdrawal(_row(withdrawal_pct=0.50), THRESHOLDS) is True
    assert sig_heavy_withdrawal(_row(withdrawal_pct=0.49), THRESHOLDS) is False


def test_heavy_withdrawal_never_fires_with_no_visible_real_withdrawal() -> None:
    """Both withdrawal slots as fee postings: no real withdrawal visible, pct is None."""
    assert sig_heavy_withdrawal(_row(withdrawal_pct=None), THRESHOLDS) is False


# --- sig_dormant ---


def test_dormant_fires_past_the_day_threshold() -> None:
    assert sig_dormant(_row(days_since_deposit=366), THRESHOLDS) is True
    assert sig_dormant(_row(days_since_deposit=365), THRESHOLDS) is False


def test_dormant_never_fires_without_a_recency_anchor() -> None:
    assert sig_dormant(_row(days_since_deposit=None), THRESHOLDS) is False


# --- sig_broken_pattern ---


def test_broken_pattern_fires_past_the_overdue_multiple() -> None:
    row = _row(typical_gap_days=30.0, days_since_deposit=91)  # just past 3x
    assert sig_broken_pattern(row, THRESHOLDS) is True
    # Exactly 3x is the boundary itself, not yet past it.
    assert (
        sig_broken_pattern(_row(typical_gap_days=30.0, days_since_deposit=90), THRESHOLDS) is False
    )


def test_broken_pattern_never_fires_with_no_reliable_pattern() -> None:
    """A single deposit gives no pattern at all -- nothing to be overdue against."""
    assert (
        sig_broken_pattern(_row(typical_gap_days=None, days_since_deposit=999), THRESHOLDS) is False
    )


# --- sig_shrinking ---


def test_shrinking_fires_on_a_steep_enough_decline() -> None:
    row = _row(deposit_trend=-0.11, days_since_deposit=10)
    assert sig_shrinking(row, THRESHOLDS) is True
    # Exactly at the slope is the boundary itself, not yet past it.
    assert sig_shrinking(_row(deposit_trend=-0.10, days_since_deposit=10), THRESHOLDS) is False


def test_shrinking_can_still_fire_for_an_already_dormant_client() -> None:
    """No dormancy exclusion: a steep enough decline fires regardless of how
    long it has been since the last deposit.
    """
    row = _row(deposit_trend=-0.50, days_since_deposit=400)
    assert sig_shrinking(row, THRESHOLDS) is True


def test_shrinking_never_fires_without_three_or_more_deposits() -> None:
    assert sig_shrinking(_row(deposit_trend=None), THRESHOLDS) is False


# --- sig_going_dormant ---


def test_going_dormant_fires_under_a_years_runway() -> None:
    assert sig_going_dormant(_row(months_until_empty=11.9), THRESHOLDS) is True
    assert sig_going_dormant(_row(months_until_empty=12.0), THRESHOLDS) is False


def test_going_dormant_never_fires_with_no_observed_fee() -> None:
    assert sig_going_dormant(_row(months_until_empty=None), THRESHOLDS) is False


# --- sig_never_repeated ---


def test_never_repeated_fires_on_exactly_one_deposit() -> None:
    assert sig_never_repeated(_row(n_deposits=1), THRESHOLDS) is True
    assert sig_never_repeated(_row(n_deposits=2), THRESHOLDS) is False
    assert sig_never_repeated(_row(n_deposits=0), THRESHOLDS) is False


# --- both capping flags together ---


def test_both_caps_still_lets_the_row_be_read() -> None:
    """A capped, history-hidden client is still a valid signal input --
    capping just means several signals stay quiet, not that the row breaks.
    """
    row = _row(
        deposit_count_capped=True,
        withdrawal_history_hidden=True,
        largest_withdrawal=None,
        withdrawal_pct=None,
        typical_gap_days=None,
        n_deposits=5,
    )
    outcomes = fired_signals(row, THRESHOLDS)
    assert outcomes["sig_heavy_withdrawal"] is False
    assert outcomes["sig_broken_pattern"] is False
    assert outcomes["sig_never_repeated"] is False


def test_every_signal_has_a_label() -> None:
    assert set(SIGNAL_LABELS) == set(SIGNAL_FUNCS)
    assert all(label.strip() for label in SIGNAL_LABELS.values())
