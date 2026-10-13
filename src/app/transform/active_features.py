"""Derive the active-book behavioural measures from flattened active_client_fund rows.
A pure function of the flattened result and a reference date, per client and fund."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median

from app.transform.active_flatten import ActiveFlattenResult, ActiveTxnRow

# Any withdrawal at or under this KES amount is a fee posting, anything above is real.
# The monthly fee is exactly this amount; delete the rule if the source adds a type code.
SYSTEM_FEE_MAX = 50.0

# The observed recurring deduction, KES, same reasoning as SYSTEM_FEE_MAX:
# read directly off the KES withdrawal amounts, not a per-client average.
# Every client's months until empty is measured against this one figure,
# not their own fee postings, since a single client rarely has enough
# visible fee history (at most two withdrawal slots) to average reliably.
FEE_PER_MONTH = 50.0

# --- frozen bucket cutoffs. Retune here, nowhere else. ---
#
# Interior cutoffs only, right-closed: a value sitting exactly on one belongs
# to the band below it. Same discipline as VALUE_BAND_CUTOFFS in
# transform/features.py.

# Fixed literal edges, KES, straight off active_eda.ipynb's own binning --
# not a recomputed statistic, so no sign-off decision was needed. The first
# and third edges match TINY_BALANCE and WORTH_A_CALL_BALANCE (KES 100 /
# 10,000) so a balance right at either threshold lands in the tier its name
# implies.
BALANCE_TIER_CUTOFFS = (100.0, 1_000.0, 10_000.0, 100_000.0, 1_000_000.0)
BALANCE_TIERS = ("Tiny", "Micro", "Small", "Core", "Premium", "Institutional")

# Fixed literal edges, days, straight off active_eda.ipynb's own binning --
# also not a recomputed statistic.
RECENCY_BAND_CUTOFFS = (30, 90, 180, 365, 730)
RECENCY_BANDS = ("<=1m", "1-3m", "3-6m", "6-12m", "1-2y", "2y+")

# Quartiles of avg_deposit_amount across active_eda_out's 27,481-row client x
# fund extract (the same population client_risk_features serves), rounded to
# clean figures rather than shipped as raw decimals. Frozen, not recomputed
# per run -- the same choice Phase 1 made for VALUE_BAND_CUTOFFS: recomputing
# quartiles live would move a client between tiers without their behaviour
# changing.
VALUE_TIER_CUTOFFS = (500.0, 3_000.0, 18_500.0)
VALUE_TIERS = ("Low", "Medium", "High", "Top")


@dataclass
class ActiveFeatureMeasures:
    """One client-fund's derived behaviour, before any bucketing.
    Balance and deposit count are carried through so the signals read one object."""

    client_id: int
    unit_fund_id: int
    balance: float | None
    n_deposits: int
    typical_gap_days: float | None
    avg_deposit_amount: float | None
    max_deposit_amount: float | None
    last_deposit_amount: float | None
    first_deposit_date: date | None
    deposit_trend: float | None
    largest_withdrawal: float | None
    # The most recent date among real withdrawals specifically -- not the
    # same as last_withdrawal_slot_date on active_client_fund, which is the
    # most recent date among every withdrawal slot including system fee
    # postings.
    last_withdrawal_date: date | None
    withdrawal_pct: float | None
    months_until_empty: float | None
    days_since_deposit: int | None
    deposit_count_capped: bool
    withdrawal_history_hidden: bool


def _typical_gap_days(dates: list[date]) -> float | None:
    """Median gap between deposits in days, counting same-day deposits once.
    Needs at least two distinct dates."""
    unique = sorted(set(dates))
    if len(unique) < 2:
        return None
    gaps = [(later - earlier).days for earlier, later in zip(unique, unique[1:], strict=False)]
    return float(median(gaps))


def _log_slope(amounts: list[float]) -> float | None:
    """Trend in deposit size on a log10 scale, positive when deposits grow.
    Needs three or more deposits."""
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


def _largest_withdrawal(withdrawals: list[ActiveTxnRow], system_fee_max: float) -> float | None:
    """The largest withdrawal above system_fee_max, or None if all are fee postings."""
    real = [w.amount for w in withdrawals if w.amount > system_fee_max]
    return max(real) if real else None


def _last_withdrawal_date(withdrawals: list[ActiveTxnRow], system_fee_max: float) -> date | None:
    """The most recent date among real withdrawals, or None if none is visible."""
    dates = [w.date for w in withdrawals if w.amount > system_fee_max and w.date is not None]
    return max(dates) if dates else None


def _withdrawal_pct(largest_withdrawal: float | None, balance: float | None) -> float | None:
    """The largest withdrawal as a share of the balance before it, rebuilt as balance plus it."""
    if largest_withdrawal is None or balance is None:
        return None
    balance_before_withdrawal = balance + largest_withdrawal
    if balance_before_withdrawal <= 0:
        return None
    return largest_withdrawal / balance_before_withdrawal


def _months_until_empty(balance: float | None, fee_per_month: float) -> float | None:
    """How many months the balance covers at the recurring deduction rate."""
    if balance is None or fee_per_month <= 0:
        return None
    return balance / fee_per_month


def _last_deposit_amount(deposits: list[ActiveTxnRow]) -> float | None:
    if not deposits:
        return None
    ordered = sorted(deposits, key=lambda t: (t.date, t.txn_id or 0))
    return ordered[-1].amount


def _days_since_deposit(last_deposit: date | None, reference_date: date | None) -> int | None:
    """Days since the last deposit, never since any transaction, and never below zero."""
    if last_deposit is None or reference_date is None:
        return None
    return max(0, (reference_date - last_deposit).days)


def _bucket(value: float | None, cutoffs: tuple[float, ...], labels: tuple[str, ...]) -> str:
    """Right-closed bucketing for the three band helpers; "Unknown" when value is missing."""
    if value is None:
        return "Unknown"
    for cutoff, label in zip(cutoffs, labels, strict=False):
        if value <= cutoff:
            return label
    return labels[-1]


def balance_tier(balance: float | None) -> str:
    return _bucket(balance, BALANCE_TIER_CUTOFFS, BALANCE_TIERS)


def recency_band(days_since_deposit: int | None) -> str:
    return _bucket(days_since_deposit, RECENCY_BAND_CUTOFFS, RECENCY_BANDS)


def value_tier(avg_deposit_amount: float | None) -> str:
    return _bucket(avg_deposit_amount, VALUE_TIER_CUTOFFS, VALUE_TIERS)


def derive_active_measures(
    result: ActiveFlattenResult,
    reference_date: datetime | None = None,
    system_fee_max: float = SYSTEM_FEE_MAX,
    fee_per_month: float = FEE_PER_MONTH,
) -> dict[tuple[int, int], ActiveFeatureMeasures]:
    """Measure each client-fund relationship in the active book.
    reference_date adds days_since_deposit. Transactions with no date are ignored."""
    ref = reference_date.date() if reference_date is not None else None

    deposits: dict[tuple[int, int], list[ActiveTxnRow]] = defaultdict(list)
    withdrawals: dict[tuple[int, int], list[ActiveTxnRow]] = defaultdict(list)
    for txn in result.transactions:
        if txn.unit_fund_id is None or txn.date is None:
            continue
        key = (txn.client_id, txn.unit_fund_id)
        (deposits if txn.txn_type == "purchase" else withdrawals)[key].append(txn)

    measures: dict[tuple[int, int], ActiveFeatureMeasures] = {}
    for row in result.clients:
        key = (row.client_id, row.unit_fund_id)
        if key in measures:
            continue
        deposited = sorted(deposits.get(key, []), key=lambda t: t.date)
        withdrawn = withdrawals.get(key, [])
        amounts = [t.amount for t in deposited]
        deposit_dates = [t.date for t in deposited]

        largest_withdrawal = _largest_withdrawal(withdrawn, system_fee_max)
        # Hidden only when the withdrawal window is full and, on top of
        # that, not one of the withdrawals it shows is a real one -- a full
        # window with at least one real withdrawal visible is capped, not
        # hidden.
        withdrawal_history_hidden = row.withdrawal_history_hidden and largest_withdrawal is None

        measures[key] = ActiveFeatureMeasures(
            client_id=row.client_id,
            unit_fund_id=row.unit_fund_id,
            balance=row.balance,
            n_deposits=row.n_deposits,
            typical_gap_days=_typical_gap_days(deposit_dates),
            avg_deposit_amount=(sum(amounts) / len(amounts)) if amounts else None,
            max_deposit_amount=max(amounts) if amounts else None,
            last_deposit_amount=_last_deposit_amount(deposited),
            first_deposit_date=deposit_dates[0] if deposit_dates else None,
            deposit_trend=_log_slope(amounts),
            largest_withdrawal=largest_withdrawal,
            last_withdrawal_date=_last_withdrawal_date(withdrawn, system_fee_max),
            withdrawal_pct=_withdrawal_pct(largest_withdrawal, row.balance),
            months_until_empty=_months_until_empty(row.balance, fee_per_month),
            days_since_deposit=_days_since_deposit(row.last_deposit_date, ref),
            deposit_count_capped=row.deposit_count_capped,
            withdrawal_history_hidden=withdrawal_history_hidden,
        )
    return measures
