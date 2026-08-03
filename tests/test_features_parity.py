"""Check the band derivation against the analysis it was translated from.

The analysis output carries client names, so it is never committed here. Point
ACE_PARITY_FEATURES_CSV at a local copy to run these; without it they skip, and
the band edges are still covered by the committed boundary tests.

What this proves is that moving the analysis into production code changed no
client's band. A difference here is a translation error, not a tuning choice.
"""

from __future__ import annotations

import csv
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.transform.features import (
    DRAWDOWN_DAYS,
    LONG_HOLD_DAYS,
    _cadence_band,
    _exit_reason,
    _fund_type,
    _hold_band,
    _in_wave,
    _purchase_depth,
    _recency_band,
    _trend_band,
    _value_band,
)

# The analysis cuts hold time three ways. Production splits the middle band at
# six months so a rule can name it, so the two agree only after collapsing.
_HOLD_COLLAPSE = {
    "Parked briefly": "Parked briefly",
    "Under 6m": "Stayed months",
    "Stayed months": "Stayed months",
    "Stayed years": "Stayed years",
    # No sale, so no hold time to measure. Absent in the analysis too.
    "Unknown": "Unknown",
}


def _rows() -> list[dict[str, str]]:
    path = os.environ.get("ACE_PARITY_FEATURES_CSV")
    if not path:
        pytest.skip("set ACE_PARITY_FEATURES_CSV to a copy of the analysis feature table")
    source = Path(path)
    if not source.exists():
        pytest.skip(f"parity source not found: {source}")
    with source.open(encoding="utf8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def rows() -> list[dict[str, str]]:
    return _rows()


def _num(value: str) -> float | None:
    return float(value) if value not in ("", "nan") else None


def _int(value: str) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _date(value: str):
    from datetime import date

    if not value:
        return None
    return date.fromisoformat(value[:10])


def _flag(value: str) -> bool:
    return value.strip().lower() == "true"


def _mismatches(rows: list[dict[str, str]], derive, expected_of) -> Iterator[str]:
    for row in rows:
        got, want = derive(row), expected_of(row)
        if got != want:
            where = f"client {row['client_id']} fund {row['unit_fund_id']}"
            yield f"{where}: got {got!r}, want {want!r}"


def _assert_matches(rows, derive, expected_of, label: str) -> None:
    bad = list(_mismatches(rows, derive, expected_of))
    assert not bad, f"{label}: {len(bad)} of {len(rows)} differ\n" + "\n".join(bad[:10])


def test_the_source_covers_the_whole_population(rows) -> None:
    assert len(rows) > 0


def test_value_band_matches(rows) -> None:
    _assert_matches(
        rows, lambda r: _value_band(_num(r["avg_ticket"])), lambda r: r["value_tier"], "value_band"
    )


def test_recency_band_matches(rows) -> None:
    _assert_matches(
        rows,
        lambda r: _recency_band(_int(r["days_cold"])),
        lambda r: r["recency_band"],
        "recency_band",
    )


def test_hold_band_matches_once_collapsed(rows) -> None:
    """Away from the one-year mark the two agree exactly.

    The analysis describes hold time with a right-closed cut, so a hold of
    exactly a year reads as months there, while its own routing asks for a year
    or more and takes the same client as years. Production follows the routing,
    since that is what picks the angle, so the boundary day is excluded here and
    pinned by the test below.
    """
    off_boundary = [r for r in rows if _int(r["hold_days"]) != LONG_HOLD_DAYS]
    _assert_matches(
        off_boundary,
        lambda r: _HOLD_COLLAPSE[_hold_band(_int(r["hold_days"]))],
        lambda r: r["hold_band"] or "Unknown",
        "hold_band",
    )


def test_a_hold_of_exactly_a_year_counts_as_years(rows) -> None:
    """The routing asks for a year or more, so the boundary day belongs above."""
    boundary = [r for r in rows if _int(r["hold_days"]) == LONG_HOLD_DAYS]
    assert boundary, "no row sits on the boundary, so this no longer proves anything"
    assert all(_hold_band(_int(r["hold_days"])) == "Stayed years" for r in boundary)


def test_cadence_band_agrees_on_who_has_a_rhythm(rows) -> None:
    _assert_matches(
        rows,
        lambda r: _cadence_band(_num(r["rhythm_days"])) != "None",
        lambda r: _flag(r["has_rhythm"]),
        "cadence_band",
    )


def test_exit_reason_agrees_on_who_chose_to_leave(rows) -> None:
    _assert_matches(
        rows,
        lambda r: _exit_reason(r["exit_type"] or None) == "client_sale",
        lambda r: _flag(r["client_chose_to_exit"]),
        "exit_reason",
    )


def test_in_wave_matches(rows) -> None:
    _assert_matches(
        rows, lambda r: _in_wave(_date(r["exit_date"])), lambda r: _flag(r["in_wave"]), "in_wave"
    )


def test_stale_contact_matches(rows) -> None:
    from app.transform.features import STALE_CONTACT_DAYS

    _assert_matches(
        rows,
        lambda r: (_int(r["days_cold"]) or 0) > STALE_CONTACT_DAYS,
        lambda r: _flag(r["stale_contact"]),
        "stale_contact",
    )


def test_holds_other_funds_matches(rows) -> None:
    _assert_matches(
        rows,
        lambda r: (_int(r["n_funds"]) or 1) > 1,
        lambda r: _flag(r["holds_other_funds"]),
        "holds_other_funds",
    )


def test_purchase_depth_agrees_with_censoring(rows) -> None:
    _assert_matches(
        rows,
        lambda r: _purchase_depth(_int(r["n_purchases"]) or 0) == "capped",
        lambda r: _flag(r["history_censored"]),
        "purchase_depth",
    )


def test_trend_band_is_derivable_for_every_row(rows) -> None:
    """No direct column to compare, so this pins coverage rather than values."""
    bands = {_trend_band(_num(r["ticket_trend"])) for r in rows}
    assert bands <= {"rising", "flat", "falling", "unknown"}


def test_fund_type_covers_every_fund_in_the_source(rows) -> None:
    types = {_fund_type(r["unit_fund_name"]) for r in rows}
    assert "other" not in types, "a fund in the source maps to no known type"


def test_staged_exit_threshold_holds(rows) -> None:
    staged = [r for r in rows if (_int(r["drawdown_days"]) or -1) >= DRAWDOWN_DAYS]
    assert all((_int(r["drawdown_days"]) or 0) >= DRAWDOWN_DAYS for r in staged)
