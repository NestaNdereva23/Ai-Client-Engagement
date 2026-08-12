"""Composing the six signals into a score, a band, and a reason string.

A weighted sum of six booleans, banded, never shipped without its reasons.
This is a priority ordering Cytonn owns and can retune, not a probability.
Weights, thresholds, and band cutoffs all come from one active
RiskConfigVersion, never hand-edited in place.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.db.models.risk import RiskConfigVersion
from app.risk.signals import SIGNAL_LABELS, SIGNAL_ORDER, fired_signals
from app.transform.active_features import ActiveFeatureMeasures
from app.transform.active_features import balance_tier as _balance_tier
from app.transform.active_features import recency_band as _recency_band
from app.transform.active_features import value_tier as _value_tier

RISK_BANDS = frozenset({"None", "Low", "Watch", "High", "Critical"})


@dataclass
class ScoreResult:
    """One client-fund's composed score, never carried alone -- risk_reasons
    and risk_band travel with it everywhere it's stored or shown.
    """

    risk_score: float
    risk_band: str
    risk_reasons: str
    aum_at_risk: float
    signals: dict[str, bool]
    recency_band: str
    balance_tier: str
    value_tier: str


def _band(score: float, cutoffs: Sequence[float]) -> str:
    """None/Low/Watch/High/Critical from four ascending cutoffs.

    Right-closed, the same convention transform/features.py's
    VALUE_BAND_CUTOFFS uses: a score sitting exactly on a cutoff belongs to
    the band below it, not the one above.
    """
    none_max, low_max, watch_max, high_max = cutoffs
    if score > high_max:
        return "Critical"
    if score > watch_max:
        return "High"
    if score > low_max:
        return "Watch"
    if score > none_max:
        return "Low"
    return "None"


def _reasons(signals: dict[str, bool]) -> str:
    """Joined labels of fired signals, in declaration order, or 'no signal'.

    A score is never shipped alone -- this string always travels with it.
    """
    fired = [SIGNAL_LABELS[name] for name in SIGNAL_ORDER if signals.get(name)]
    return "; ".join(fired) if fired else "no signal"


def compose_score(row: ActiveFeatureMeasures, config: RiskConfigVersion) -> ScoreResult:
    """Turn one client-fund's row into a score, band, and reasons.

    Deterministic: the same row and the same config version always produce
    the same result.
    """
    signals = fired_signals(row, config.thresholds)
    score = sum(int(signals[name]) * config.weights[name] for name in SIGNAL_ORDER)
    band = _band(score, config.thresholds["RISK_BAND_CUTOFFS"])
    reasons = _reasons(signals)
    aum_at_risk = (row.balance or 0.0) * score / 100

    return ScoreResult(
        risk_score=score,
        risk_band=band,
        risk_reasons=reasons,
        aum_at_risk=aum_at_risk,
        signals=signals,
        recency_band=_recency_band(row.days_since_purchase),
        balance_tier=_balance_tier(row.balance),
        value_tier=_value_tier(row.avg_ticket),
    )
