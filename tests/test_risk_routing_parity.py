"""Check the router against the analysis it was translated from.

The analysis output carries client names, so it is never committed here.
Point ACE_PARITY_ROUTES_CSV at a local copy of the analysis's feature table
(client_features.csv) to run these; without it they skip, and the route
distribution is still covered by the pinned counts below plus the
committed precedence/capacity/override tests in test_risk_routing.py.

What this proves is that moving routing into production code changed no
client's route. A difference here is a translation error, not a tuning
choice -- the analysis has no complaint or suppression data, so this
parity check only exercises the signal-only base routing (AM7.1, AM7.2).
"""

from __future__ import annotations

import csv
import os
from collections import Counter
from pathlib import Path

import pytest

from app.db.models.risk import RiskConfigVersion
from app.risk.routing import RoutableRow, route_population

# From route_summary.csv. Pinned as a literal so the count assertion holds
# even when the source file, which carries client names, is not available.
EXPECTED_ROUTE_COUNTS = {
    "monitor_only": 18709,
    "fa_call_priority": 150,
    "fa_digest_watch": 1493,
    "automated_nurture": 4604,
    "dust_cleanup": 2525,
}

THRESHOLDS = {"DUST_BALANCE": 100, "MATERIAL_BALANCE": 10_000}


def _config() -> RiskConfigVersion:
    return RiskConfigVersion(thresholds=THRESHOLDS, fa_call_capacity=150, at_risk_min=25)


def _rows() -> list[dict[str, str]]:
    path = os.environ.get("ACE_PARITY_ROUTES_CSV")
    if not path:
        pytest.skip("set ACE_PARITY_ROUTES_CSV to a copy of the analysis feature table")
    source = Path(path)
    if not source.exists():
        pytest.skip(f"parity source not found: {source}")
    with source.open(encoding="utf8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def rows() -> list[dict[str, str]]:
    return _rows()


def _flag(value: str) -> bool:
    return value.strip().lower() == "true"


@pytest.fixture(scope="module")
def routed(rows) -> dict[tuple[int, int], tuple[dict[str, str], str, str]]:
    """Every source row alongside the route production routing gives it,
    computed once for the whole module.
    """
    routable = [
        RoutableRow(
            key=(int(r["client_id"]), int(r["unit_fund_id"])),
            balance=float(r["balance"]),
            risk_score=float(r["risk_score"]),
            sig_dormant=_flag(r["sig_dormant"]),
            aum_at_risk=float(r["aum_at_risk"]),
        )
        for r in rows
    ]
    results = route_population(routable, _config())
    return {
        row.key: (source_row, results[row.key].route, source_row["route"])
        for row, source_row in zip(routable, rows, strict=True)
    }


def test_the_source_covers_the_whole_population(rows) -> None:
    assert len(rows) > 0


def test_every_row_routes_the_way_the_analysis_did(routed) -> None:
    bad = [
        f"client {source_row['client_id']} fund {source_row['unit_fund_id']}: "
        f"got {got!r}, analysis gave {want!r}"
        for source_row, got, want in routed.values()
        if got != want
    ]
    assert not bad, f"{len(bad)} of {len(routed)} rows diverge\n" + "\n".join(bad[:10])


def test_the_route_counts_match_the_published_summary(routed) -> None:
    got = Counter(got for _source_row, got, _want in routed.values())
    assert dict(got) == EXPECTED_ROUTE_COUNTS
