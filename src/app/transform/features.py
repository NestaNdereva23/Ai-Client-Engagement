"""Derive per-client features from the flattened normalized rows.

Everything model facing here is a bucket or a label, never an exact amount or
date, so the projection can allow-list these columns directly. Derivation is a
pure function of the flattened result, so the same input and reference timestamp
always give the same features.

Measures are computed per client and fund, since the same person uses a cash
fund and a long-horizon fund differently. The bands describe the relationship
they are contacted on, which is their largest.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from statistics import median

from app.transform.flatten import PURCHASE_CAP, ClientRow, FlattenResult

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

# --- thresholds behind the behavioural bands. Retune here, nowhere else. ---

# Months in which exits cluster far above the usual rate.
WAVE_START = (2023, 9)
WAVE_END = (2024, 6)

# How long the money stayed after the final top-up, in days.
SHORT_HOLD_DAYS = 60
MID_HOLD_DAYS = 180
LONG_HOLD_DAYS = 365

# Two sales this far apart is a wind-down rather than one change of mind.
DRAWDOWN_DAYS = 180

# Slope of log10 contribution size beyond which the trend is real.
TREND_EPS = 0.15

# A median purchase gap at or under this is a savings cadence.
TIGHT_RHYTHM_DAYS = 45
CADENCE_REGULAR_DAYS = 90
CADENCE_PERIODIC_DAYS = 365

# Contact details older than this need checking before they are used.
STALE_CONTACT_DAYS = 1095

# Quartile boundaries of the average contribution across the population, taken
# once and frozen. Recomputing them per run would move a client between bands
# without their behaviour changing. Boundaries are right-closed: a value sitting
# exactly on one belongs to the band below.
VALUE_BAND_CUTOFFS = (3_661.0, 10_750.0, 44_120.0)

# A client's history counts as deep at three purchases, or at six months
# between the first and last one we can see.
DEPTH_PURCHASES = 3
DEPTH_WINDOW_DAYS = 180

# The values each band can take. The rule store validates against these, so a
# rule can only name a value the derivation actually produces. Each band carries
# an explicit unknown member rather than allowing a null.
RECENCY_BANDS = frozenset({"Under 1y", "1 to 3y", "3 to 6y", "Over 6y", "Unknown"})
VALUE_BANDS = frozenset({"Low", "Medium", "High", "Top"})
CADENCE_BANDS = frozenset({"None", "Tight", "Regular", "Periodic", "Infrequent"})
HOLD_BANDS = frozenset({"Parked briefly", "Under 6m", "Stayed months", "Stayed years", "Unknown"})
PURCHASE_DEPTHS = frozenset({"none", "single", "few", "capped"})
TREND_BANDS = frozenset({"rising", "flat", "falling", "unknown"})
EXIT_REASONS = frozenset({"client_sale", "charge_settled", "unknown"})
FUND_TYPES = frozenset({"money_market", "high_yield", "other"})


@dataclass
class RelationshipMeasures:
    """What one client did in one fund, before any bucketing."""

    client_id: int
    unit_fund_id: int
    avg_ticket: float | None
    max_ticket: float | None
    rhythm_days: float | None
    first_purchase: date | None
    active_window_days: int | None
    ticket_trend: float | None
    first_sale: date | None
    drawdown_days: int | None
    hold_days: int | None
    exit_type: str | None


@dataclass
class FeatureRow:
    client_id: int
    archetype: str
    recency_bucket: str
    value_tier: str
    own_rhythm_days: int | None
    rhythm_band: str
    # A count of purchases, not a value. client_fund.observed_volume is the value.
    observed_volume: int
    purchases_censored: bool
    history_censored: bool
    n_funds: int
    recency_band: str
    value_band: str
    cadence_band: str
    hold_band: str
    purchase_depth: str
    trend_band: str
    exit_reason: str
    fund_type: str
    in_wave: bool
    has_depth: bool
    staged_exit: bool
    stale_contact: bool
    holds_other_funds: bool


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


def _median_gap(dates: list[date]) -> float | None:
    """Median gap between consecutive purchases, keeping same-day repeats.

    Repeats are kept because several top-ups booked on one date are exactly what
    makes a gap of zero, and that has to stay visible: it is not a cadence.
    """
    if len(dates) < 2:
        return None
    ordered = sorted(dates)
    gaps = [(later - earlier).days for earlier, later in zip(ordered, ordered[1:], strict=False)]
    return float(median(gaps))


def _log_slope(amounts: list[float]) -> float | None:
    """Trend in contribution size across the visible purchases, on a log10 scale.

    Positive means each contribution was getting bigger. Needs three points
    before it means anything.
    """
    if len(amounts) < 3:
        return None
    ys = [math.log10(max(a, 1.0)) for a in amounts]
    n = len(ys)
    mean_x = (n - 1) / 2
    mean_y = sum(ys) / n
    variance = sum((x - mean_x) ** 2 for x in range(n))
    if variance == 0:
        return None
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in enumerate(ys))
    return covariance / variance


def _recency_band(days: int | None) -> str:
    if days is None:
        return "Unknown"
    if days <= 365:
        return "Under 1y"
    if days <= 1095:
        return "1 to 3y"
    if days <= 2190:
        return "3 to 6y"
    return "Over 6y"


def _value_band(avg_ticket: float | None) -> str:
    low, mid, high = VALUE_BAND_CUTOFFS
    if avg_ticket is None:
        return "Low"
    if avg_ticket > high:
        return "Top"
    if avg_ticket > mid:
        return "High"
    if avg_ticket > low:
        return "Medium"
    return "Low"


def _cadence_band(rhythm_days: float | None) -> str:
    """A gap under a day is same-day top-ups, so it counts as no cadence."""
    if rhythm_days is None or rhythm_days < 1:
        return "None"
    if rhythm_days <= TIGHT_RHYTHM_DAYS:
        return "Tight"
    if rhythm_days <= CADENCE_REGULAR_DAYS:
        return "Regular"
    if rhythm_days <= CADENCE_PERIODIC_DAYS:
        return "Periodic"
    return "Infrequent"


def _hold_band(hold_days: int | None) -> str:
    if hold_days is None:
        return "Unknown"
    if hold_days <= SHORT_HOLD_DAYS:
        return "Parked briefly"
    if hold_days <= MID_HOLD_DAYS:
        return "Under 6m"
    if hold_days < LONG_HOLD_DAYS:
        return "Stayed months"
    return "Stayed years"


def _purchase_depth(n_purchases: int) -> str:
    """capped means the window is full, so the real count is five or more."""
    if n_purchases >= PURCHASE_CAP:
        return "capped"
    if n_purchases >= 2:
        return "few"
    if n_purchases == 1:
        return "single"
    return "none"


def _trend_band(ticket_trend: float | None) -> str:
    if ticket_trend is None:
        return "unknown"
    if ticket_trend >= TREND_EPS:
        return "rising"
    if ticket_trend <= -TREND_EPS:
        return "falling"
    return "flat"


def _exit_reason(exit_type: str | None) -> str:
    """Whether the balance went to zero by the client's choice or by a charge."""
    if exit_type == "unit_sale":
        return "client_sale"
    if exit_type in ("bill_payment", "interest"):
        return "charge_settled"
    return "unknown"


def _fund_type(fund_name: str | None) -> str:
    name = (fund_name or "").lower()
    if "money market" in name:
        return "money_market"
    if "high yield" in name:
        return "high_yield"
    return "other"


def _in_wave(exit_date: date | None) -> bool:
    if exit_date is None:
        return False
    return WAVE_START <= (exit_date.year, exit_date.month) <= WAVE_END


def largest_first(rows: list[ClientRow]) -> list[ClientRow]:
    """One client's relationships, largest observed purchase volume first.

    The lowest fund id breaks a tie, so the same input always picks the same
    relationship to contact on.
    """
    return sorted(rows, key=lambda r: (-r.total_purchase_amount, r.unit_fund_id))


def relationships_by_client(result: FlattenResult) -> dict[int, list[ClientRow]]:
    """Group the flattened rows by client, one entry per fund they hold.

    A pair repeated across pages keeps its last occurrence, matching what the
    upsert would leave behind.
    """
    unique: dict[tuple[int, int], ClientRow] = {
        (row.client_id, row.unit_fund_id): row for row in result.clients
    }
    by_client: dict[int, list[ClientRow]] = defaultdict(list)
    for row in unique.values():
        by_client[row.client_id].append(row)
    return by_client


def derive_relationship_measures(
    result: FlattenResult,
) -> dict[tuple[int, int], RelationshipMeasures]:
    """Measure each client-fund relationship from its transactions.

    Anything needing an order ignores undated transactions, since a date we
    could not parse cannot be placed in the sequence.
    """
    purchases: dict[tuple[int, int], list] = defaultdict(list)
    sales: dict[tuple[int, int], list] = defaultdict(list)
    for txn in result.transactions:
        if txn.unit_fund_id is None or txn.date is None:
            continue
        key = (txn.client_id, txn.unit_fund_id)
        (purchases if txn.txn_type == "purchase" else sales)[key].append(txn)

    measures: dict[tuple[int, int], RelationshipMeasures] = {}
    for row in result.clients:
        key = (row.client_id, row.unit_fund_id)
        if key in measures:
            continue
        bought = sorted(purchases.get(key, []), key=lambda t: t.date)
        sold = sorted(sales.get(key, []), key=lambda t: t.date)
        amounts = [t.amount for t in bought]
        buy_dates = [t.date for t in bought]
        sell_dates = [t.date for t in sold]

        window = (buy_dates[-1] - buy_dates[0]).days if buy_dates else None
        drawdown = (sell_dates[-1] - sell_dates[0]).days if sell_dates else None
        hold = None
        if buy_dates and sell_dates:
            hold = max((sell_dates[-1] - buy_dates[-1]).days, 0)

        measures[key] = RelationshipMeasures(
            client_id=row.client_id,
            unit_fund_id=row.unit_fund_id,
            avg_ticket=(sum(amounts) / len(amounts)) if amounts else None,
            max_ticket=max(amounts) if amounts else None,
            rhythm_days=_median_gap(buy_dates),
            first_purchase=buy_dates[0] if buy_dates else None,
            active_window_days=window,
            ticket_trend=_log_slope(amounts),
            first_sale=sell_dates[0] if sell_dates else None,
            drawdown_days=drawdown,
            hold_days=hold,
            exit_type=sold[-1].sale_type if sold else None,
        )
    return measures


def _has_depth(n_purchases: int, active_window_days: int | None) -> bool:
    if n_purchases >= DEPTH_PURCHASES:
        return True
    return active_window_days is not None and active_window_days >= DEPTH_WINDOW_DAYS


def derive_features(
    result: FlattenResult,
    measures: Mapping[tuple[int, int], RelationshipMeasures] | None = None,
) -> list[FeatureRow]:
    """Turn one flattened result into one feature row per client.

    The bucket columns aggregate every fund a client holds. The behavioural
    bands describe the one relationship they are contacted on, which is their
    largest, so a band never mixes two funds used for different purposes.
    """
    if measures is None:
        measures = derive_relationship_measures(result)
    by_client = relationships_by_client(result)
    fund_names = {fund.unit_fund_id: fund.unit_fund_name for fund in result.funds}

    purchase_dates: dict[int, list[date]] = defaultdict(list)
    for txn in result.transactions:
        if txn.txn_type == "purchase" and txn.date is not None:
            purchase_dates[txn.client_id].append(txn.date)

    features: list[FeatureRow] = []
    for client_id, rows in by_client.items():
        ordered = largest_first(rows)
        primary = ordered[0]
        measure = measures[(primary.client_id, primary.unit_fund_id)]

        observed_volume = sum(r.n_purchases_returned for r in ordered)
        total_purchase = sum(r.total_purchase_amount for r in ordered)
        recencies = [
            r.days_since_last_activity for r in ordered if r.days_since_last_activity is not None
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
                purchases_censored=any(r.purchases_censored for r in ordered),
                history_censored=any(r.history_censored for r in ordered),
                n_funds=len(ordered),
                recency_band=_recency_band(primary.days_since_last_activity),
                value_band=_value_band(measure.avg_ticket),
                cadence_band=_cadence_band(measure.rhythm_days),
                hold_band=_hold_band(measure.hold_days),
                purchase_depth=_purchase_depth(primary.n_purchases_returned),
                trend_band=_trend_band(measure.ticket_trend),
                exit_reason=_exit_reason(measure.exit_type),
                fund_type=_fund_type(fund_names.get(primary.unit_fund_id)),
                in_wave=_in_wave(primary.last_activity_date),
                has_depth=_has_depth(primary.n_purchases_returned, measure.active_window_days),
                staged_exit=measure.drawdown_days is not None
                and measure.drawdown_days >= DRAWDOWN_DAYS,
                stale_contact=primary.days_since_last_activity is not None
                and primary.days_since_last_activity > STALE_CONTACT_DAYS,
                holds_other_funds=len(ordered) > 1,
            )
        )
    return features
