"""Tests for the six risk signals against constructed edge-case rows."""

from __future__ import annotations

from app.risk.signals import (
    SIGNAL_FUNCS,
    SIGNAL_LABELS,
    fired_signals,
    sig_cadence_break,
    sig_dormant,
    sig_drawdown,
    sig_fee_erosion,
    sig_never_repeated,
    sig_shrinking,
)
from app.transform.active_features import ActiveFeatureMeasures

THRESHOLDS = {
    "DORMANT_DAYS": 365,
    "DRAWDOWN_HEAVY": 0.50,
    "LAPSE_MULTIPLE": 3.0,
    "DECLINE_SLOPE": -0.10,
    "DUST_BALANCE": 100,
    "MATERIAL_BALANCE": 10_000,
    "FEE_RUNWAY_MONTHS": 12,
    "FEE_PER_MONTH": 50,
    "SYSTEM_SALE_MAX": 100,
}


def _row(**overrides) -> ActiveFeatureMeasures:
    base = dict(
        client_id=1,
        unit_fund_id=10,
        balance=500_000.0,
        n_purchases=3,
        rhythm_days=30.0,
        avg_ticket=50_000.0,
        max_ticket=100_000.0,
        last_ticket=50_000.0,
        ticket_trend=0.0,
        largest_real_sale=None,
        last_real_sale_date=None,
        drawdown_ratio=None,
        fee_runway_months=None,
        days_since_purchase=10,
        purchases_censored=False,
        redemption_history_blind=False,
    )
    base.update(overrides)
    return ActiveFeatureMeasures(**base)


# --- sig_drawdown ---


def test_drawdown_fires_at_or_above_the_heavy_threshold() -> None:
    assert sig_drawdown(_row(drawdown_ratio=0.50), THRESHOLDS) is True
    assert sig_drawdown(_row(drawdown_ratio=0.49), THRESHOLDS) is False


def test_drawdown_never_fires_with_no_visible_real_redemption() -> None:
    """Both sale slots as fee postings: no real sale visible, ratio is None."""
    assert sig_drawdown(_row(drawdown_ratio=None), THRESHOLDS) is False


# --- sig_dormant ---


def test_dormant_fires_past_the_day_threshold() -> None:
    assert sig_dormant(_row(days_since_purchase=366), THRESHOLDS) is True
    assert sig_dormant(_row(days_since_purchase=365), THRESHOLDS) is False


def test_dormant_never_fires_without_a_recency_anchor() -> None:
    assert sig_dormant(_row(days_since_purchase=None), THRESHOLDS) is False


# --- sig_cadence_break ---


def test_cadence_break_fires_past_the_lapse_multiple() -> None:
    row = _row(rhythm_days=30.0, days_since_purchase=91)  # just past 3x
    assert sig_cadence_break(row, THRESHOLDS) is True
    # Exactly 3x is the boundary itself, not yet past it.
    assert sig_cadence_break(_row(rhythm_days=30.0, days_since_purchase=90), THRESHOLDS) is False


def test_cadence_break_never_fires_with_no_credible_rhythm() -> None:
    """A single purchase gives no rhythm at all -- nothing to be overdue against."""
    assert sig_cadence_break(_row(rhythm_days=None, days_since_purchase=999), THRESHOLDS) is False


# --- sig_shrinking ---


def test_shrinking_fires_on_a_steep_enough_decline() -> None:
    row = _row(ticket_trend=-0.11, days_since_purchase=10)
    assert sig_shrinking(row, THRESHOLDS) is True
    # Exactly at the slope is the boundary itself, not yet past it.
    assert sig_shrinking(_row(ticket_trend=-0.10, days_since_purchase=10), THRESHOLDS) is False


def test_shrinking_can_still_fire_for_an_already_dormant_client() -> None:
    """No dormancy exclusion: a steep enough decline fires regardless of how
    long it has been since the last purchase.
    """
    row = _row(ticket_trend=-0.50, days_since_purchase=400)
    assert sig_shrinking(row, THRESHOLDS) is True


def test_shrinking_never_fires_without_three_or_more_purchases() -> None:
    assert sig_shrinking(_row(ticket_trend=None), THRESHOLDS) is False


# --- sig_fee_erosion ---


def test_fee_erosion_fires_under_a_years_runway() -> None:
    assert sig_fee_erosion(_row(fee_runway_months=11.9), THRESHOLDS) is True
    assert sig_fee_erosion(_row(fee_runway_months=12.0), THRESHOLDS) is False


def test_fee_erosion_never_fires_with_no_observed_fee() -> None:
    assert sig_fee_erosion(_row(fee_runway_months=None), THRESHOLDS) is False


# --- sig_never_repeated ---


def test_never_repeated_fires_on_exactly_one_purchase() -> None:
    assert sig_never_repeated(_row(n_purchases=1), THRESHOLDS) is True
    assert sig_never_repeated(_row(n_purchases=2), THRESHOLDS) is False
    assert sig_never_repeated(_row(n_purchases=0), THRESHOLDS) is False


# --- both truncation caps together ---


def test_both_caps_still_lets_the_row_be_read() -> None:
    """A censored, redemption-blind client is still a valid signal input --
    truncation just means several signals stay quiet, not that the row breaks.
    """
    row = _row(
        purchases_censored=True,
        redemption_history_blind=True,
        largest_real_sale=None,
        drawdown_ratio=None,
        rhythm_days=None,
        n_purchases=5,
    )
    outcomes = fired_signals(row, THRESHOLDS)
    assert outcomes["sig_drawdown"] is False
    assert outcomes["sig_cadence_break"] is False
    assert outcomes["sig_never_repeated"] is False


def test_every_signal_has_a_label() -> None:
    assert set(SIGNAL_LABELS) == set(SIGNAL_FUNCS)
    assert all(label.strip() for label in SIGNAL_LABELS.values())
