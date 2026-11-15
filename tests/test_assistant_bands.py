from __future__ import annotations

import pytest

from app.briefing.assistant_bands import (
    contact_age_band,
    contact_count_band,
    deposit_rhythm_band,
    withdrawal_recency_band,
    withdrawal_size_band,
)


@pytest.mark.parametrize(
    ("share", "band"),
    [
        (None, "none"),
        (0, "none"),
        (0.05, "small"),
        (0.2, "moderate"),
        (0.49, "moderate"),
        (0.603, "large"),
        (0.75, "nearly_all"),
        (0.99, "nearly_all"),
    ],
)
def test_withdrawal_size_bands(share, band) -> None:
    assert withdrawal_size_band(share) == band


@pytest.mark.parametrize(
    ("days", "band"),
    [(None, "none"), (0, "this_week"), (7, "this_week"), (8, "this_month"), (31, "this_month")]
    + [(32, "last_three_months"), (92, "last_three_months"), (93, "older")],
)
def test_withdrawal_recency_bands(days, band) -> None:
    assert withdrawal_recency_band(days) == band


def test_a_client_never_contacted_is_never_not_none() -> None:
    assert contact_age_band(None) == "never"
    assert contact_age_band(3) == "this_week"


@pytest.mark.parametrize(
    ("gap", "band"),
    [
        (None, "unknown"),
        (0.4, "unknown"),
        (5, "weekly"),
        (30, "monthly"),
        (67, "every_two_months"),
        (100, "quarterly"),
        (200, "rarely"),
    ],
)
def test_deposit_rhythm_bands(gap, band) -> None:
    assert deposit_rhythm_band(gap) == band


@pytest.mark.parametrize(
    ("count", "band"), [(0, "none"), (1, "one"), (2, "several"), (9, "several")]
)
def test_contact_count_bands(count, band) -> None:
    assert contact_count_band(count) == band
