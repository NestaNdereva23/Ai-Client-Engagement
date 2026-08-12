"""Boundary behaviour of the behavioural bands.

Each band is cut so that a rule can name it instead of comparing numbers, which
means the cut points have to sit exactly where the rules expect. These pin the
edges, including the ones that are easy to get wrong: bands are right-closed, so
a value sitting exactly on a cut belongs to the band below.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.transform.features import (
    VALUE_BAND_CUTOFFS,
    _cadence_band,
    _exit_reason,
    _fund_type,
    _has_depth,
    _hold_band,
    _in_wave,
    _log_slope,
    _median_gap,
    _purchase_depth,
    _recency_band,
    _trend_band,
    _value_band,
)

LOW, MID, HIGH = VALUE_BAND_CUTOFFS


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (None, "Unknown"),
        (0, "Under 1y"),
        (365, "Under 1y"),
        (366, "1 to 3y"),
        (1095, "1 to 3y"),
        (1096, "3 to 6y"),
        (2190, "3 to 6y"),
        (2191, "Over 6y"),
    ],
)
def test_recency_band_edges(days: int | None, expected: str) -> None:
    assert _recency_band(days) == expected


@pytest.mark.parametrize(
    ("avg_ticket", "expected"),
    [
        (None, "Low"),
        (0, "Low"),
        (LOW, "Low"),
        (LOW + 0.01, "Medium"),
        (MID, "Medium"),
        (MID + 0.01, "High"),
        (HIGH, "High"),
        (HIGH + 0.01, "Top"),
    ],
)
def test_value_band_edges(avg_ticket: float | None, expected: str) -> None:
    assert _value_band(avg_ticket) == expected


@pytest.mark.parametrize(
    ("rhythm", "expected"),
    [
        (None, "None"),
        (0, "None"),
        (0.5, "None"),
        (1, "Tight"),
        (45, "Tight"),
        (46, "Regular"),
        (90, "Regular"),
        (91, "Periodic"),
        (365, "Periodic"),
        (366, "Infrequent"),
    ],
)
def test_cadence_band_edges(rhythm: float | None, expected: str) -> None:
    assert _cadence_band(rhythm) == expected


@pytest.mark.parametrize(
    ("hold", "expected"),
    [
        (None, "Unknown"),
        (0, "Under 2m"),
        (60, "Under 2m"),
        (61, "Under 6m"),
        (180, "Under 6m"),
        (181, "Stayed months"),
        (364, "Stayed months"),
        (365, "Stayed years"),
    ],
)
def test_hold_band_edges(hold: int | None, expected: str) -> None:
    assert _hold_band(hold) == expected


@pytest.mark.parametrize(
    ("n", "expected"),
    [(0, "none"), (1, "single"), (2, "few"), (4, "few"), (5, "capped")],
)
def test_purchase_depth_edges(n: int, expected: str) -> None:
    assert _purchase_depth(n) == expected


@pytest.mark.parametrize(
    ("trend", "expected"),
    [
        (None, "unknown"),
        (0.15, "rising"),
        (0.14, "flat"),
        (0.0, "flat"),
        (-0.14, "flat"),
        (-0.15, "falling"),
    ],
)
def test_trend_band_edges(trend: float | None, expected: str) -> None:
    assert _trend_band(trend) == expected


@pytest.mark.parametrize(
    ("exit_type", "expected"),
    [
        ("unit_sale", "client_sale"),
        ("bill_payment", "charge_settled"),
        ("interest", "charge_settled"),
        (None, "unknown"),
        ("something_else", "unknown"),
    ],
)
def test_exit_reason_mapping(exit_type: str | None, expected: str) -> None:
    assert _exit_reason(exit_type) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Cytonn Money Market Fund", "money_market"),
        ("Cytonn High Yield Fund", "high_yield"),
        ("Balanced Fund", "other"),
        (None, "other"),
    ],
)
def test_fund_type_mapping(name: str | None, expected: str) -> None:
    assert _fund_type(name) == expected


@pytest.mark.parametrize(
    ("exit_date", "expected"),
    [
        # A spike month: any day inside it counts, not just the boundary days.
        (date(2023, 9, 1), True),
        (date(2023, 9, 30), True),
        (date(2026, 7, 4), True),
        # Adjacent months that are not themselves spikes.
        (date(2023, 8, 31), False),
        (date(2023, 10, 1), False),
        # Inside the old contiguous window, but not a spike on its own: the
        # membership test only recognises the nine months WAVE_MONTHS names.
        (date(2023, 12, 1), False),
        (date(2024, 6, 30), False),
        (None, False),
    ],
)
def test_in_wave_edges(exit_date: date | None, expected: bool) -> None:
    assert _in_wave(exit_date) is expected


def test_in_wave_recognises_every_frozen_spike_month() -> None:
    from app.transform.features import WAVE_MONTHS

    for year, month in WAVE_MONTHS:
        assert _in_wave(date(year, month, 15)) is True


@pytest.mark.parametrize(
    ("n_purchases", "window", "expected"),
    [
        (3, None, True),
        (2, 180, True),
        (2, 179, False),
        (1, None, False),
        (2, None, False),
    ],
)
def test_has_depth_takes_either_route(n_purchases: int, window: int | None, expected: bool) -> None:
    assert _has_depth(n_purchases, window) is expected


def test_median_gap_keeps_same_day_repeats() -> None:
    """Three top-ups on one day is a gap of zero, not an absent cadence."""
    day = date(2024, 1, 1)
    assert _median_gap([day, day, day]) == 0.0


def test_median_gap_needs_two_purchases() -> None:
    assert _median_gap([date(2024, 1, 1)]) is None
    assert _median_gap([]) is None


def test_log_slope_needs_three_points() -> None:
    assert _log_slope([100.0, 200.0]) is None


def test_log_slope_signs_the_direction() -> None:
    rising = _log_slope([100.0, 1_000.0, 10_000.0])
    falling = _log_slope([10_000.0, 1_000.0, 100.0])
    flat = _log_slope([500.0, 500.0, 500.0])
    assert rising is not None and rising > 0
    assert falling is not None and falling < 0
    assert flat == 0.0


def test_log_slope_survives_a_zero_amount() -> None:
    """log10(0) is undefined, so amounts are floored at one before the fit."""
    assert _log_slope([0.0, 100.0, 1_000.0]) is not None
