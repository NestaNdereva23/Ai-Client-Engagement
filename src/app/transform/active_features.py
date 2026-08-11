"""Derive the active-book behavioural dimensions from flattened active_client_fund rows.

These are the measures the risk signals need: how recently and how regularly a
client contributes, whether their ticket size is trending down, how deep their
last real withdrawal cut into their balance, and how long their balance covers
the fees being taken from it. Derivation is a pure function of the flattened
result (plus a reference date for recency), so the same input always gives the
same output.

Measures are computed per client and fund, matching how one active_client_fund
row already represents one client-fund relationship.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median

from app.transform.active_flatten import ActiveFlattenResult, ActiveTxnRow

# The source has no transaction type code for sales yet, so a real client
# redemption can't be told apart from a system fee posting directly. This
# threshold is a stand-in: any sale at or under it is treated as a fee
# posting, anything above it as a real withdrawal. It is a guess, not a
# measurement -- the day the real transaction type code is available, this
# heuristic should be deleted, not retuned.
SYSTEM_SALE_MAX = 100.0


@dataclass
class ActiveFeatureMeasures:
    """One client-fund's derived behaviour, before any bucketing."""

    client_id: int
    unit_fund_id: int
    rhythm_days: float | None
    avg_ticket: float | None
    max_ticket: float | None
    last_ticket: float | None
    ticket_trend: float | None
    largest_real_sale: float | None
    drawdown_ratio: float | None
    fee_runway_months: float | None
    days_since_purchase: int | None
    purchases_censored: bool
    redemption_history_blind: bool


def _rhythm_days(dates: list[date]) -> float | None:
    """Median gap between purchases, in days.

    Purchases booked on the same date are deduplicated first, so a same-day
    batch of top-ups never counts as a gap of zero. Needs at least two
    distinct dates to mean anything.
    """
    unique = sorted(set(dates))
    if len(unique) < 2:
        return None
    gaps = [(later - earlier).days for earlier, later in zip(unique, unique[1:], strict=False)]
    return float(median(gaps))


def _log_slope(amounts: list[float]) -> float | None:
    """Trend in ticket size across purchases, on a log10 scale.

    Positive means each purchase was bigger than the last. Needs three or
    more points before a trend means anything; a move from KES 100k to 10k
    and one from 10k to 1k register the same slope, since both are a 10x drop.
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


def _classify_sales(
    sales: list[ActiveTxnRow], system_sale_max: float
) -> tuple[float | None, float | None]:
    """Split a client's visible sales into a real redemption and a fee estimate.

    A sale at or under system_sale_max is a system fee posting, not something
    the client asked for. If every visible sale is a fee posting, no real
    redemption is visible at all -- largest_real_sale comes back None even
    though the sale slots may be full.
    """
    real = [s.amount for s in sales if s.amount > system_sale_max]
    fees = [s.amount for s in sales if s.amount <= system_sale_max]
    largest_real_sale = max(real) if real else None
    avg_fee = (sum(fees) / len(fees)) if fees else None
    return largest_real_sale, avg_fee


def _drawdown_ratio(largest_real_sale: float | None, balance: float | None) -> float | None:
    """The largest real sale as a share of the balance implied before it happened.

    There is no observed prior balance, only the current one, so it is
    reconstructed by adding the withdrawal back: balance + largest_real_sale.
    """
    if largest_real_sale is None or balance is None:
        return None
    implied_prior_balance = balance + largest_real_sale
    if implied_prior_balance <= 0:
        return None
    return largest_real_sale / implied_prior_balance


def _fee_runway_months(balance: float | None, avg_fee: float | None) -> float | None:
    """How many months the balance covers at the observed recurring deduction."""
    if balance is None or avg_fee is None or avg_fee <= 0:
        return None
    return balance / avg_fee


def _last_ticket(purchases: list[ActiveTxnRow]) -> float | None:
    if not purchases:
        return None
    ordered = sorted(purchases, key=lambda t: (t.date, t.txn_id or 0))
    return ordered[-1].amount


def _days_since_purchase(last_purchase: date | None, reference_date: date | None) -> int | None:
    """Days since the client's last purchase, never since any transaction.

    A sale doesn't reset this: a client who only ever withdraws is not
    "recently active" in the sense that matters for a win-back signal.
    """
    if last_purchase is None or reference_date is None:
        return None
    return (reference_date - last_purchase).days


def derive_active_measures(
    result: ActiveFlattenResult,
    reference_date: datetime | None = None,
    system_sale_max: float = SYSTEM_SALE_MAX,
) -> dict[tuple[int, int], ActiveFeatureMeasures]:
    """Measure each client-fund relationship in the active book.

    reference_date is optional: the measures persisted to active_client_fund
    don't need it, but a caller anchoring a risk run's recency signal can pass
    it to get days_since_purchase back as well. Transactions with no date are
    ignored, since a date we could not parse can't be placed in the sequence.
    """
    ref = reference_date.date() if reference_date is not None else None

    purchases: dict[tuple[int, int], list[ActiveTxnRow]] = defaultdict(list)
    sales: dict[tuple[int, int], list[ActiveTxnRow]] = defaultdict(list)
    for txn in result.transactions:
        if txn.unit_fund_id is None or txn.date is None:
            continue
        key = (txn.client_id, txn.unit_fund_id)
        (purchases if txn.txn_type == "purchase" else sales)[key].append(txn)

    measures: dict[tuple[int, int], ActiveFeatureMeasures] = {}
    for row in result.clients:
        key = (row.client_id, row.unit_fund_id)
        if key in measures:
            continue
        bought = sorted(purchases.get(key, []), key=lambda t: t.date)
        sold = sales.get(key, [])
        amounts = [t.amount for t in bought]
        buy_dates = [t.date for t in bought]

        largest_real_sale, avg_fee = _classify_sales(sold, system_sale_max)

        measures[key] = ActiveFeatureMeasures(
            client_id=row.client_id,
            unit_fund_id=row.unit_fund_id,
            rhythm_days=_rhythm_days(buy_dates),
            avg_ticket=(sum(amounts) / len(amounts)) if amounts else None,
            max_ticket=max(amounts) if amounts else None,
            last_ticket=_last_ticket(bought),
            ticket_trend=_log_slope(amounts),
            largest_real_sale=largest_real_sale,
            drawdown_ratio=_drawdown_ratio(largest_real_sale, row.balance),
            fee_runway_months=_fee_runway_months(row.balance, avg_fee),
            days_since_purchase=_days_since_purchase(row.last_purchase, ref),
            purchases_censored=row.purchases_censored,
            redemption_history_blind=row.redemption_history_blind,
        )
    return measures
