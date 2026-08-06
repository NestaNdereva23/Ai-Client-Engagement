"""Tests for deriving bucketed client features from flattened rows.

These are pure: they flatten a crafted payload and check the derived buckets, the
carried censoring flags, and that the same input gives the same features.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.transform.features import derive_features
from app.transform.flatten import flatten_payload

EAT = timezone(timedelta(hours=3))
# Anchored well after the sample dates so recency buckets are predictable.
ANCHOR = datetime(2026, 7, 23, 9, 0, tzinfo=EAT)


def _payload(
    purchases: list[tuple[int, str, str]],
    sales: list[tuple[int, str, str]] | None = None,
) -> dict[str, Any]:
    """One client. Each txn tuple is (id, date, amount)."""
    return {
        "data": [
            {
                "unit_fund_id": 10,
                "unit_fund_name": "Money Market Fund",
                "inactive_client_count": 1,
                "clients": [
                    {
                        "client_id": 1001,
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


def _only(payload: dict[str, Any]):
    features = derive_features(flatten_payload(payload, ANCHOR))
    assert len(features) == 1
    return features[0]


def test_rhythm_from_gaps_between_purchases() -> None:
    purchases = [
        (1, "2024-01-01T00:00:00", "100"),
        (2, "2024-02-01T00:00:00", "100"),  # 31 days
        (3, "2024-03-01T00:00:00", "100"),  # 29 days
    ]
    f = _only(_payload(purchases))
    assert f.own_rhythm_days == 30


def test_rhythm_unknown_with_single_purchase() -> None:
    f = _only(_payload([(1, "2024-01-01T00:00:00", "100")]))
    assert f.own_rhythm_days is None


def test_history_censored_when_sales_window_full() -> None:
    f = _only(
        _payload(
            [(1, "2024-01-01T00:00:00", "100")],
            sales=[(50, "2024-01-01T00:00:00", "50"), (51, "2024-02-01T00:00:00", "50")],
        )
    )
    assert f.purchases_censored is False
    assert f.history_censored is True


def test_derivation_is_deterministic() -> None:
    payload = _payload([(1, "2024-01-01T00:00:00", "100"), (2, "2024-06-01T00:00:00", "100")])
    first = derive_features(flatten_payload(payload, ANCHOR))
    second = derive_features(flatten_payload(payload, ANCHOR))
    assert first == second
