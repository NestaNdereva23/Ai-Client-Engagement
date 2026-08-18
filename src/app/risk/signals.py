"""The six risk signals.

Each is a pure function over one client-fund's ActiveFeatureMeasures and the
active thresholds dict from RiskConfigVersion. A signal a client's own data
can't speak to (a missing pattern, no visible real withdrawal, and so on)
returns False rather than guessing: a signal is only allowed to fire on
evidence actually present in the row.

These are rules, not a model, because each one has to survive being explained
to a compliance reviewer, and each has to point at a different action. A
weighted sum of these six booleans is composed into a score elsewhere; this
module only decides whether each one fires.
"""

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

    A client with no reliable pattern (fewer than two deposit dates) has
    nothing to be overdue against, so this never fires for them.
    """
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


def fired_signals(row: ActiveFeatureMeasures, thresholds: Thresholds) -> dict[str, bool]:
    """Every signal's outcome for one row, keyed by signal name."""
    return {name: func(row, thresholds) for name, func in SIGNAL_FUNCS.items()}


def fired_signal_tags(row: object) -> list[str]:
    """Fired signal names for one already-scored row, "sig_" stripped, in
    SIGNAL_ORDER -- the same order risk_reasons is joined in, and the same
    short codes wherever this is computed (ClientRiskFeatures, RiskSnapshot,
    DigestLine's own build step), so a client-fund reads the same tags
    whether it showed up in today's digest, its risk history, or its
    current bands. row is anything exposing the six sig_* attributes as
    booleans -- an ORM row here, never the ScoreResult.signals dict itself.
    """
    return [name.removeprefix("sig_") for name in SIGNAL_ORDER if getattr(row, name)]
