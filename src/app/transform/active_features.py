"""Derive the active-book behavioural dimensions from flattened active_client_fund rows.

These are the measures the risk signals need: how recently and how regularly a
client contributes, whether their deposit size is trending down, how deep their
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
from app.transform.flatten import SALE_CAP as WITHDRAWAL_CAP

# The source has no transaction type code for withdrawals yet, so a real
# client withdrawal can't be told apart from a system fee posting directly.
# This threshold is a stand-in: any withdrawal at or under it is treated as
# a fee posting, anything above it as a real withdrawal. It is a guess, not
# a measurement -- the day the real transaction type code is available,
# this rule of thumb should be deleted, not retuned.
#
# This is already a KES figure, not a USD one converted like
# VALUE_BAND_CUTOFFS: the observed automatic fee postings sit around KES 50,
# so KES 100 is a cap set just above that, read directly off the KES
# withdrawal amounts.
SYSTEM_FEE_MAX = 100.0

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

    balance and n_deposits are carried straight through from the flattened
    row, alongside the derived measures, so the risk signals have everything
    they need from one object.
    """

    client_id: int
    unit_fund_id: int
    balance: float | None
    n_deposits: int
    typical_gap_days: float | None
    avg_deposit_amount: float | None
    max_deposit_amount: float | None
    last_deposit_amount: float | None
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
    """Median gap between deposits, in days.

    Deposits booked on the same date are deduplicated first, so a same-day
    batch of top-ups never counts as a gap of zero. Needs at least two
    distinct dates to mean anything.
    """
    unique = sorted(set(dates))
    if len(unique) < 2:
        return None
    gaps = [(later - earlier).days for earlier, later in zip(unique, unique[1:], strict=False)]
    return float(median(gaps))


def _log_slope(amounts: list[float]) -> float | None:
    """Trend in deposit size across deposits, on a log10 scale.

    Positive means each deposit was bigger than the last. Needs three or
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


def _largest_withdrawal(withdrawals: list[ActiveTxnRow], system_fee_max: float) -> float | None:
    """The largest withdrawal worth more than system_fee_max, or None if
    every visible withdrawal is at or under it -- a system fee posting, not
    something the client asked for.
    """
    real = [w.amount for w in withdrawals if w.amount > system_fee_max]
    return max(real) if real else None


def _last_withdrawal_date(withdrawals: list[ActiveTxnRow], system_fee_max: float) -> date | None:
    """The most recent date among real withdrawals, or None if none is visible.

    Independent of _largest_withdrawal: the most recent real withdrawal is
    not necessarily the largest one, and a briefing reports both facts as
    they are, not as if they were the same transaction.
    """
    dates = [w.date for w in withdrawals if w.amount > system_fee_max and w.date is not None]
    return max(dates) if dates else None


def _withdrawal_pct(largest_withdrawal: float | None, balance: float | None) -> float | None:
    """The largest withdrawal as a share of the balance implied before it happened.

    There is no observed prior balance, only the current one, so it is
    reconstructed by adding the withdrawal back: balance + largest_withdrawal.
    """
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
    """Days since the client's last deposit, never since any transaction.

    A withdrawal doesn't reset this: a client who only ever withdraws is not
    "recently active" in the sense that matters for a win-back signal.
    Clipped at zero, so a deposit dated after the reference point (a data
    quirk, not a real future deposit) never reads as negative.
    """
    if last_deposit is None or reference_date is None:
        return None
    return max(0, (reference_date - last_deposit).days)


def _bucket(value: float | None, cutoffs: tuple[float, ...], labels: tuple[str, ...]) -> str:
    """Right-closed bucketing shared by balance_tier/recency_band/value_tier.

    "Unknown" when value is missing -- never guessed into a real tier.
    """
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

    reference_date is optional: the measures persisted to active_client_fund
    don't need it, but a caller anchoring a risk run's recency signal can pass
    it to get days_since_deposit back as well. Transactions with no date are
    ignored, since a date we could not parse can't be placed in the sequence.
    """
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
        withdrawal_history_hidden = len(withdrawn) >= WITHDRAWAL_CAP and largest_withdrawal is None

        measures[key] = ActiveFeatureMeasures(
            client_id=row.client_id,
            unit_fund_id=row.unit_fund_id,
            balance=row.balance,
            n_deposits=row.n_deposits,
            typical_gap_days=_typical_gap_days(deposit_dates),
            avg_deposit_amount=(sum(amounts) / len(amounts)) if amounts else None,
            max_deposit_amount=max(amounts) if amounts else None,
            last_deposit_amount=_last_deposit_amount(deposited),
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
