"""The six risk signals.

Each is a pure function over one client-fund's ActiveFeatureMeasures and the
active thresholds dict from RiskConfigVersion. A signal a client's own data
can't speak to (a missing rhythm, no visible real sale, and so on) returns
False rather than guessing: a signal is only allowed to fire on evidence
actually present in the row.

These are rules, not a model, because each one has to survive being explained
to a compliance reviewer, and each has to point at a different action. A
weighted sum of these six booleans is composed into a score elsewhere; this
module only decides whether each one fires.
"""

from __future__ import annotations

from app.transform.active_features import ActiveFeatureMeasures

Thresholds = dict[str, float]


def sig_drawdown(row: ActiveFeatureMeasures, thresholds: Thresholds) -> bool:
    """A real redemption worth at least DRAWDOWN_HEAVY of the implied prior balance."""
    if row.drawdown_ratio is None:
        return False
    return row.drawdown_ratio >= thresholds["DRAWDOWN_HEAVY"]


def sig_dormant(row: ActiveFeatureMeasures, thresholds: Thresholds) -> bool:
    """No purchase in over DORMANT_DAYS."""
    if row.days_since_purchase is None:
        return False
    return row.days_since_purchase > thresholds["DORMANT_DAYS"]


def sig_cadence_break(row: ActiveFeatureMeasures, thresholds: Thresholds) -> bool:
    """Overdue past LAPSE_MULTIPLE times their own rhythm.

    A client with no credible rhythm (fewer than two purchase dates) has
    nothing to be overdue against, so this never fires for them.
    """
    if row.rhythm_days is None or row.days_since_purchase is None:
        return False
    return row.days_since_purchase > thresholds["LAPSE_MULTIPLE"] * row.rhythm_days


def sig_shrinking(row: ActiveFeatureMeasures, thresholds: Thresholds) -> bool:
    """Ticket size trending down in log space."""
    if row.ticket_trend is None:
        return False
    return row.ticket_trend < thresholds["DECLINE_SLOPE"]


def sig_fee_erosion(row: ActiveFeatureMeasures, thresholds: Thresholds) -> bool:
    """Balance covers under FEE_RUNWAY_MONTHS of the observed recurring fee."""
    if row.fee_runway_months is None:
        return False
    return row.fee_runway_months < thresholds["FEE_RUNWAY_MONTHS"]


def sig_never_repeated(row: ActiveFeatureMeasures, thresholds: Thresholds) -> bool:
    """Exactly one purchase, ever."""
    return row.n_purchases == 1


# Declaration order, matching the notebook's own section order (9.1 to 9.6),
# not weight order. risk/scoring.py joins fired reasons in this order, so a
# reason string is reproducible the same way the notebook's own is.
SIGNAL_FUNCS = {
    "sig_cadence_break": sig_cadence_break,
    "sig_dormant": sig_dormant,
    "sig_drawdown": sig_drawdown,
    "sig_shrinking": sig_shrinking,
    "sig_fee_erosion": sig_fee_erosion,
    "sig_never_repeated": sig_never_repeated,
}
SIGNAL_ORDER = tuple(SIGNAL_FUNCS)

# The notebook's own wording, verbatim, since this is the text an FA reads.
SIGNAL_LABELS = {
    "sig_cadence_break": "Broke their own cadence",
    "sig_dormant": "No contribution in 12m",
    "sig_drawdown": "Heavy redemption",
    "sig_shrinking": "Shrinking deposits",
    "sig_fee_erosion": "Fees will empty the account",
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
