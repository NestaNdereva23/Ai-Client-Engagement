"""Derive per-client features from the flattened normalized rows.

Everything here is a bucket or a label, never an exact amount or date, so the
model-facing projection can allow-list these columns directly. Derivation is a
pure function of the flattened result, so the same input and reference timestamp
always give the same features.

The threshold numbers are placeholders, kept as named constants so they are easy
to tune once the real distribution of the population is confirmed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from statistics import median

from app.transform.flatten import PURCHASE_CAP, FlattenResult

# value_tier cutoffs on total purchase amount.
VALUE_TIER_TOP = 1_000_000
VALUE_TIER_HIGH = 250_000
VALUE_TIER_MID = 50_000

# recency_bucket cutoffs in days since last activity.
RECENCY_1Y = 365
RECENCY_2Y = 730
RECENCY_3Y = 1095

# rhythm_band cutoffs on the typical gap between purchases, in days.
RHYTHM_REGULAR = 90
RHYTHM_PERIODIC = 365


@dataclass
class FeatureRow:
    client_id: int
    archetype: str
    recency_bucket: str
    value_tier: str
    own_rhythm_days: int | None
    rhythm_band: str
    observed_volume: int
    purchases_censored: bool
    history_censored: bool


def _archetype(volume: int) -> str:
    if volume >= PURCHASE_CAP:
        return "Frequent (5+, censored)"
    if volume >= 2:
        return "Occasional (2-4)"
    if volume == 1:
        return "One-and-done"
    return "None observed"


def _recency_bucket(days: int | None) -> str:
    if days is None:
        return "Unknown"
    if days < RECENCY_1Y:
        return "Exited under 1y"
    if days < RECENCY_2Y:
        return "Exited 1 to 2y"
    if days < RECENCY_3Y:
        return "Exited 2 to 3y"
    return "Exited 3y plus"


def _value_tier(total_purchase: float) -> str:
    if total_purchase >= VALUE_TIER_TOP:
        return "Top"
    if total_purchase >= VALUE_TIER_HIGH:
        return "High"
    if total_purchase >= VALUE_TIER_MID:
        return "Mid"
    return "Low"


def _rhythm_days(dates: list[date]) -> int | None:
    """Typical gap in days between purchases, or None with fewer than two."""
    unique = sorted(set(dates))
    if len(unique) < 2:
        return None
    gaps = [(later - earlier).days for earlier, later in zip(unique, unique[1:], strict=False)]
    return int(median(gaps))


def _rhythm_band(days: int | None) -> str:
    if days is None:
        return "Unknown"
    if days <= RHYTHM_REGULAR:
        return "Regular"
    if days <= RHYTHM_PERIODIC:
        return "Periodic"
    return "Infrequent"


def derive_features(result: FlattenResult) -> list[FeatureRow]:
    """Turn one flattened result into one feature row per client.

    Values are aggregated per client id, so a client holding several funds gets a
    single feature row covering all of them.
    """
    rows_by_client: dict[int, list] = defaultdict(list)
    for client in result.clients:
        rows_by_client[client.client_id].append(client)

    purchase_dates: dict[int, list[date]] = defaultdict(list)
    for txn in result.transactions:
        if txn.txn_type == "purchase" and txn.date is not None:
            purchase_dates[txn.client_id].append(txn.date)

    features: list[FeatureRow] = []
    for client_id, rows in rows_by_client.items():
        observed_volume = sum(r.n_purchases_returned for r in rows)
        total_purchase = sum(r.total_purchase_amount for r in rows)
        recencies = [
            r.days_since_last_activity for r in rows if r.days_since_last_activity is not None
        ]
        days_since = min(recencies) if recencies else None
        rhythm = _rhythm_days(purchase_dates.get(client_id, []))
        features.append(
            FeatureRow(
                client_id=client_id,
                archetype=_archetype(observed_volume),
                recency_bucket=_recency_bucket(days_since),
                value_tier=_value_tier(total_purchase),
                own_rhythm_days=rhythm,
                rhythm_band=_rhythm_band(rhythm),
                observed_volume=observed_volume,
                purchases_censored=any(r.purchases_censored for r in rows),
                history_censored=any(r.history_censored for r in rows),
            )
        )
    return features
