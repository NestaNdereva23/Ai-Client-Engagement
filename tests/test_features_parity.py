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
from collections import Counter
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from app.db.session import SessionLocal
from app.rules.engine import resolve
from app.rules.store import load_active_rules
from app.transform.features import (
    DRAWDOWN_DAYS,
    LONG_HOLD_DAYS,
    _cadence_band,
    _exit_reason,
    _fund_type,
    _has_depth,
    _hold_band,
    _in_wave,
    _priority_tier,
    _purchase_depth,
    _recency_band,
    _trend_band,
    _value_band,
)

# Inside v3's real window: 2026-08-04 to 2026-08-11, closed by 209a9c997624
# when v4 took over for the hold_band rename.
V3_IN_FORCE = date(2026, 8, 10)

# From angle_summary.csv. Pinned as a literal so the count assertion holds
# even when the source file, which carries client names, is not available.
EXPECTED_ANGLE_COUNTS = {
    "see_what_changed": 1258,
    "your_next_deposit": 733,
    "the_long_hold": 543,
    "onboarding_retry": 345,
    "pick_up_again": 308,
    "back_on_schedule": 300,
    "you_wound_down": 275,
    "wrong_shelf": 215,
    "you_were_fading": 183,
    "you_were_scaling": 169,
    "second_try": 138,
    "not_a_goodbye": 30,
}

_TIER_DISPLAY = {"T1": "Tier 1 top", "T2": "Tier 2 high", "T3": "Tier 3 medium", "T4": "Tier 4 low"}
EXPECTED_TIER_COUNTS = {"T1": 1003, "T2": 1207, "T3": 1108, "T4": 1179}

# The analysis cuts hold time three ways. Production splits the middle band at
# six months so a rule can name it, so the two agree only after collapsing.
# Keys are production's _hold_band() output; values are the analysis CSV's own
# column, which still says "Parked briefly" since it is a frozen historical
# extract, not re-run against production's post-rename vocabulary.
_HOLD_COLLAPSE = {
    "Under 2m": "Parked briefly",
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


def _tier_of(row: dict[str, str]) -> str:
    value_band = _value_band(_num(row["avg_ticket"]))
    recency_band = _recency_band(_int(row["days_cold"]))
    return _priority_tier(value_band, recency_band)


def test_priority_tier_matches(rows) -> None:
    _assert_matches(
        rows, lambda r: _TIER_DISPLAY[_tier_of(r)], lambda r: r["priority_tier"], "priority_tier"
    )


def test_the_four_tier_counts_match_the_published_summary(rows) -> None:
    got = Counter(_tier_of(r) for r in rows)
    assert dict(got) == EXPECTED_TIER_COUNTS


# --- the router, end to end: real bands, real seeded rules, real engine ---


def _routing_features(row: dict[str, str]) -> dict[str, str]:

    n_purchases = _int(row["n_purchases"]) or 0
    drawdown = _int(row["drawdown_days"])
    return {
        "exit_reason": _exit_reason(row["exit_type"] or None),
        "fund_type": _fund_type(row["unit_fund_name"]),
        "hold_band": _hold_band(_int(row["hold_days"])),
        "in_wave": "true" if _in_wave(_date(row["exit_date"])) else "false",
        "has_depth": "true"
        if _has_depth(n_purchases, _int(row["active_window_days"]))
        else "false",
        "purchase_depth": _purchase_depth(n_purchases),
        "staged_exit": "true" if drawdown is not None and drawdown >= DRAWDOWN_DAYS else "false",
        "trend_band": _trend_band(_num(row["ticket_trend"])),
        "cadence_band": _cadence_band(_num(row["rhythm_days"])),
    }


@pytest.fixture
def v3_rule_set(db: None):
    # Function-scoped because it depends on the function-scoped db fixture;
    # loading twelve rows is cheap enough that re-running it per test costs
    # nothing worth caching.
    with SessionLocal() as session:
        rules = load_active_rules(session, at=V3_IN_FORCE)
    assert rules, "the v3 rule set must be active on the date used for this harness"
    return rules


@pytest.fixture
def routed(rows, v3_rule_set) -> list[tuple[dict[str, str], str]]:
    """Every row alongside the angle the real engine assigns it, computed once."""
    return [(row, resolve(_routing_features(row), v3_rule_set).message_angle) for row in rows]


def test_every_row_routes_to_the_angle_the_analysis_assigned(routed) -> None:
    bad = [
        f"client {row['client_id']} fund {row['unit_fund_id']}: "
        f"engine gave {got!r}, analysis gave {row['message_angle']!r}"
        for row, got in routed
        if got != row["message_angle"]
    ]
    assert not bad, f"{len(bad)} of {len(routed)} rows diverge\n" + "\n".join(bad[:10])


def test_the_twelve_angle_counts_match_the_published_summary(routed) -> None:
    got = Counter(angle for _row, angle in routed)
    assert dict(got) == EXPECTED_ANGLE_COUNTS


def test_every_row_gets_exactly_one_angle_from_the_catalogue(routed) -> None:
    assert set(EXPECTED_ANGLE_COUNTS) == set(angle for _row, angle in routed)
    assert sum(EXPECTED_ANGLE_COUNTS.values()) == len(routed)
