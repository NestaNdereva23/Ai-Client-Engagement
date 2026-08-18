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
        last_deposit=None,
        overdue_multiple=None,
        largest_withdrawal=None,
        balance=None,
        deposit_trend=None,
        months_until_empty=None,
        reference_date=date(2026, 6, 1),
    )
    base.update(overrides)
    return base


def test_no_fired_signal_returns_none() -> None:
    assert primary_signal_magnitude(**_row()) is None


def test_dormant_signal_reports_days_since_deposit() -> None:
    signals = dict(NO_SIGNALS, sig_dormant=True)
    result = primary_signal_magnitude(**_row(signals=signals, last_deposit=date(2026, 1, 1)))
    assert result == "No deposit in 12 months: 151 days since last deposit"


def test_heavy_withdrawal_signal_reports_percent_of_balance() -> None:
    signals = dict(NO_SIGNALS, sig_heavy_withdrawal=True)
    result = primary_signal_magnitude(
        **_row(signals=signals, largest_withdrawal=63_000.0, balance=37_000.0)
    )
    assert result == "Heavy withdrawal: 63% of balance withdrawn at once"


def test_going_dormant_signal_reports_months_until_empty() -> None:
    signals = dict(NO_SIGNALS, sig_going_dormant=True)
    result = primary_signal_magnitude(**_row(signals=signals, months_until_empty=4.2))
    assert result == "Fees will empty the account: 4.2 months until the balance empties"


def test_never_repeated_signal_has_a_fixed_phrase() -> None:
    signals = dict(NO_SIGNALS, sig_never_repeated=True)
    result = primary_signal_magnitude(**_row(signals=signals))
    assert result == "Never made a second deposit: only one deposit, ever"


def test_picks_the_highest_weighted_fired_signal_not_declaration_order() -> None:
    # sig_dormant is declared before sig_going_dormant in SIGNAL_ORDER, but a
    # config that weighs going-dormant higher should surface that one.
    signals = dict(NO_SIGNALS, sig_dormant=True, sig_going_dormant=True)
    weights = {name: 0 for name in SIGNAL_ORDER}
    weights["sig_dormant"] = 10
    weights["sig_going_dormant"] = 90
    result = primary_signal_magnitude(
        **_row(
            signals=signals,
            weights=weights,
            last_deposit=date(2026, 1, 1),
            months_until_empty=2.0,
        )
    )
    assert result.startswith("Fees will empty the account")


def test_ties_break_by_declaration_order() -> None:
    signals = dict(NO_SIGNALS, sig_dormant=True, sig_going_dormant=True)
    result = primary_signal_magnitude(
        **_row(
            signals=signals,
            last_deposit=date(2026, 1, 1),
            months_until_empty=2.0,
        )
    )
    assert result.startswith("No deposit in 12 months")


def test_missing_number_falls_back_to_the_label_alone() -> None:
    signals = dict(NO_SIGNALS, sig_dormant=True)
    result = primary_signal_magnitude(**_row(signals=signals, last_deposit=None))
    assert result == "No deposit in 12 months"
