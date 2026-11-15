"""The six risk signals: pure functions over one client-fund's measures and the active thresholds.
A signal the data cannot speak to returns False; the score that weighs them is built elsewhere."""

from __future__ import annotations

from app.transform.active_features import ActiveFeatureMeasures

Thresholds = dict[str, float]


def sig_heavy_withdrawal(row: ActiveFeatureMeasures, thresholds: Thresholds) -> bool:
    """A real withdrawal worth at least HEAVY_WITHDRAWAL_PCT of the balance before it."""
    if row.withdrawal_pct is None:
        return False
    return row.withdrawal_pct >= thresholds["HEAVY_WITHDRAWAL_PCT"]


def sig_dormant(row: ActiveFeatureMeasures, thresholds: Thresholds) -> bool:
    """No deposit in over DORMANT_DAYS."""
    if row.days_since_deposit is None:
        return False
    return row.days_since_deposit > thresholds["DORMANT_DAYS"]


def sig_broken_pattern(row: ActiveFeatureMeasures, thresholds: Thresholds) -> bool:
    """Overdue past OVERDUE_MULTIPLE times their own deposit pattern.
    Never fires for a client with fewer than two deposit dates."""
    if row.typical_gap_days is None or row.days_since_deposit is None:
        return False
    return row.days_since_deposit > thresholds["OVERDUE_MULTIPLE"] * row.typical_gap_days


def sig_shrinking(row: ActiveFeatureMeasures, thresholds: Thresholds) -> bool:
    """Deposit size trending down in log space."""
    if row.deposit_trend is None:
        return False
    return row.deposit_trend < thresholds["SHRINKING_TREND"]


def sig_going_dormant(row: ActiveFeatureMeasures, thresholds: Thresholds) -> bool:
    """Balance covers under MONTHS_UNTIL_EMPTY of the observed recurring fee."""
    if row.months_until_empty is None:
        return False
    return row.months_until_empty < thresholds["MONTHS_UNTIL_EMPTY"]


def sig_never_repeated(row: ActiveFeatureMeasures, thresholds: Thresholds) -> bool:
    """Exactly one deposit, ever."""
    return row.n_deposits == 1


# Declaration order, matching the notebook's own section order (9.1 to 9.6),
# not weight order. risk/scoring.py joins fired reasons in this order, so a
# reason string is reproducible the same way the notebook's own is.
SIGNAL_FUNCS = {
    "sig_broken_pattern": sig_broken_pattern,
    "sig_dormant": sig_dormant,
    "sig_heavy_withdrawal": sig_heavy_withdrawal,
    "sig_shrinking": sig_shrinking,
    "sig_going_dormant": sig_going_dormant,
    "sig_never_repeated": sig_never_repeated,
}
SIGNAL_ORDER = tuple(SIGNAL_FUNCS)

# The notebook's own wording, verbatim, since this is the text an FA reads.
SIGNAL_LABELS = {
    "sig_broken_pattern": "Broke their own pattern",
    "sig_dormant": "No deposit in 12 months",
    "sig_heavy_withdrawal": "Heavy withdrawal",
    "sig_shrinking": "Shrinking deposits",
    "sig_going_dormant": "Fees will empty the account",
    "sig_never_repeated": "Never made a second deposit",
}


NEAR_PRESERVATION_LINE = "near_preservation_line"
GOING_DORMANT = "sig_going_dormant"

ADVISOR_SIGNAL_ORDER = (
    *(name for name in SIGNAL_ORDER if name != GOING_DORMANT),
    NEAR_PRESERVATION_LINE,
)
ADVISOR_SIGNAL_LABELS = {
    **SIGNAL_LABELS,
    NEAR_PRESERVATION_LINE: "Balance heading below the active line",
}


def fired_signals(row: ActiveFeatureMeasures, thresholds: Thresholds) -> dict[str, bool]:
    """Every signal's outcome for one row, keyed by signal name."""
    return {name: func(row, thresholds) for name, func in SIGNAL_FUNCS.items()}


def fired_signal_tags(row: object) -> list[str]:
    """Fired signal names for one scored row, "sig_" stripped, in SIGNAL_ORDER.
    The row is anything exposing the six sig_* attributes as booleans."""
    return [name.removeprefix("sig_") for name in SIGNAL_ORDER if getattr(row, name)]


def advisor_signal_tags(row: object, near_preservation_line: bool) -> list[str]:
    tags = [
        name.removeprefix("sig_")
        for name in SIGNAL_ORDER
        if name != GOING_DORMANT and getattr(row, name)
    ]
    return [*tags, NEAR_PRESERVATION_LINE] if near_preservation_line else tags
