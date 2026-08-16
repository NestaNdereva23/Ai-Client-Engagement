"""Tests for app.risk.magnitude.primary_signal_magnitude: picking the
fired signal that weighs heaviest in a client's own config, and rendering
its magnitude text.
"""

from __future__ import annotations

from datetime import date

from app.risk.magnitude import primary_signal_magnitude
from app.risk.signals import SIGNAL_ORDER

NO_SIGNALS = {name: False for name in SIGNAL_ORDER}
EQUAL_WEIGHTS = {name: 100 / len(SIGNAL_ORDER) for name in SIGNAL_ORDER}


def _row(**overrides):
    base = dict(
        signals=dict(NO_SIGNALS),
        weights=dict(EQUAL_WEIGHTS),
        last_purchase=None,
        lapse_ratio=None,
        largest_real_sale=None,
        balance=None,
        ticket_trend=None,
        fee_runway_months=None,
        reference_date=date(2026, 6, 1),
    )
    base.update(overrides)
    return base


def test_no_fired_signal_returns_none() -> None:
    assert primary_signal_magnitude(**_row()) is None


def test_dormant_signal_reports_days_since_purchase() -> None:
    signals = dict(NO_SIGNALS, sig_dormant=True)
    result = primary_signal_magnitude(**_row(signals=signals, last_purchase=date(2026, 1, 1)))
    assert result == "No contribution in 12m: 151 days since last purchase"


def test_drawdown_signal_reports_percent_of_balance() -> None:
    signals = dict(NO_SIGNALS, sig_drawdown=True)
    result = primary_signal_magnitude(
        **_row(signals=signals, largest_real_sale=63_000.0, balance=37_000.0)
    )
    assert result == "Heavy redemption: 63% of balance withdrawn in one sale"


def test_fee_erosion_signal_reports_months_of_runway() -> None:
    signals = dict(NO_SIGNALS, sig_fee_erosion=True)
    result = primary_signal_magnitude(**_row(signals=signals, fee_runway_months=4.2))
    assert result == "Fees will empty the account: 4.2 months of fee runway left"


def test_never_repeated_signal_has_a_fixed_phrase() -> None:
    signals = dict(NO_SIGNALS, sig_never_repeated=True)
    result = primary_signal_magnitude(**_row(signals=signals))
    assert result == "Never made a second deposit: only one purchase, ever"


def test_picks_the_highest_weighted_fired_signal_not_declaration_order() -> None:
    # sig_dormant is declared before sig_fee_erosion in SIGNAL_ORDER, but a
    # config that weighs fee erosion higher should surface that one.
    signals = dict(NO_SIGNALS, sig_dormant=True, sig_fee_erosion=True)
    weights = {name: 0 for name in SIGNAL_ORDER}
    weights["sig_dormant"] = 10
    weights["sig_fee_erosion"] = 90
    result = primary_signal_magnitude(
        **_row(
            signals=signals,
            weights=weights,
            last_purchase=date(2026, 1, 1),
            fee_runway_months=2.0,
        )
    )
    assert result.startswith("Fees will empty the account")


def test_ties_break_by_declaration_order() -> None:
    signals = dict(NO_SIGNALS, sig_dormant=True, sig_fee_erosion=True)
    result = primary_signal_magnitude(
        **_row(
            signals=signals,
            last_purchase=date(2026, 1, 1),
            fee_runway_months=2.0,
        )
    )
    assert result.startswith("No contribution in 12m")


def test_missing_number_falls_back_to_the_label_alone() -> None:
    signals = dict(NO_SIGNALS, sig_dormant=True)
    result = primary_signal_magnitude(**_row(signals=signals, last_purchase=None))
    assert result == "No contribution in 12m"
