"""Tests for compose_score: weighted sum, band, reasons, and fund_at_risk.

Pure: builds an unpersisted RiskConfigVersion row directly, no database
involved.
"""

from __future__ import annotations

from app.db.models.risk import RiskConfigVersion
from app.risk.scoring import RISK_BANDS, _band, compose_score
from app.transform.active_features import ActiveFeatureMeasures

WEIGHTS = {
    "sig_heavy_withdrawal": 30,
    "sig_dormant": 25,
    "sig_broken_pattern": 20,
    "sig_shrinking": 15,
    "sig_going_dormant": 7,
    "sig_never_repeated": 3,
}
THRESHOLDS = {
    "DORMANT_DAYS": 365,
    "HEAVY_WITHDRAWAL_PCT": 0.50,
    "OVERDUE_MULTIPLE": 3.0,
    "SHRINKING_TREND": -0.10,
    "TINY_BALANCE": 100,
    "WORTH_A_CALL_BALANCE": 10_000,
    "MONTHS_UNTIL_EMPTY": 12,
    "FEE_PER_MONTH": 50,
    "SYSTEM_FEE_MAX": 100,
    "RISK_BAND_CUTOFFS": [0, 24, 49, 74],
}
CUTOFFS = THRESHOLDS["RISK_BAND_CUTOFFS"]


def _config() -> RiskConfigVersion:
    return RiskConfigVersion(weights=WEIGHTS, thresholds=THRESHOLDS)


def _row(**overrides) -> ActiveFeatureMeasures:
    base = dict(
        client_id=1,
        unit_fund_id=10,
        balance=500_000.0,
        n_deposits=3,
        typical_gap_days=30.0,
        avg_deposit_amount=50_000.0,
        max_deposit_amount=100_000.0,
        last_deposit_amount=50_000.0,
        deposit_trend=0.0,
        largest_withdrawal=None,
        last_withdrawal_date=None,
        withdrawal_pct=None,
        months_until_empty=None,
        days_since_deposit=10,
        deposit_count_capped=False,
        withdrawal_history_hidden=False,
    )
    base.update(overrides)
    return ActiveFeatureMeasures(**base)


def test_no_signal_scores_zero_and_bands_none() -> None:
    result = compose_score(_row(), _config())
    assert result.risk_score == 0
    assert result.risk_band == "None"
    assert result.risk_reasons == "no signal"
    assert result.fund_at_risk == 0.0


def test_compose_score_fills_the_tier_columns() -> None:
    # balance=500_000 -> Premium, avg_deposit_amount=50_000 -> Top,
    # days_since_deposit=10 -> <=1m.
    result = compose_score(_row(), _config())
    assert result.balance_tier == "Premium"
    assert result.value_tier == "Top"
    assert result.recency_band == "<=1m"


def test_compose_score_tiers_are_unknown_when_the_underlying_measure_is_missing() -> None:
    row = _row(balance=None, avg_deposit_amount=None, days_since_deposit=None)
    result = compose_score(row, _config())
    assert result.balance_tier == "Unknown"
    assert result.value_tier == "Unknown"
    assert result.recency_band == "Unknown"


def test_score_is_the_weighted_sum_of_fired_signals() -> None:
    # dormant (25) + never_repeated (3) fire; nothing else does.
    row = _row(days_since_deposit=400, n_deposits=1, typical_gap_days=None)
    result = compose_score(row, _config())
    assert result.risk_score == 28


def test_all_six_signals_firing_gives_the_full_100() -> None:
    row = _row(
        withdrawal_pct=0.9,
        days_since_deposit=1000,
        typical_gap_days=10.0,
        deposit_trend=-0.9,
        months_until_empty=1.0,
        n_deposits=1,
    )
    result = compose_score(row, _config())
    assert result.risk_score == 100
    assert result.risk_band == "Critical"


def test_reasons_are_joined_in_declaration_order_not_weight_order() -> None:
    # broken_pattern, dormant, heavy_withdrawal all fire -- declaration
    # order puts broken_pattern first even though heavy_withdrawal carries
    # the higher weight.
    row = _row(withdrawal_pct=0.9, days_since_deposit=1000, typical_gap_days=10.0)
    result = compose_score(row, _config())
    assert result.risk_reasons == (
        "Broke their own pattern; No deposit in 12 months; Heavy withdrawal"
    )


def test_fund_at_risk_matches_the_formula_exactly() -> None:
    row = _row(balance=1_000_000.0, days_since_deposit=400, n_deposits=1, typical_gap_days=None)
    result = compose_score(row, _config())
    assert result.risk_score == 28
    assert result.fund_at_risk == 1_000_000.0 * 28 / 100


def test_fund_at_risk_treats_a_missing_balance_as_zero() -> None:
    row = _row(balance=None, days_since_deposit=400, n_deposits=1, typical_gap_days=None)
    result = compose_score(row, _config())
    assert result.fund_at_risk == 0.0


def test_a_band_assignment_is_always_consistent_with_its_score() -> None:
    """compose_score's own band always matches what _band alone would give
    the same score -- the two are never allowed to disagree.
    """
    row = _row(days_since_deposit=400, n_deposits=1, typical_gap_days=None)  # score 28
    result = compose_score(row, _config())
    assert result.risk_band == _band(result.risk_score, CUTOFFS)


# --- band boundaries: every documented cutoff, both sides ---


def test_band_boundary_none_to_low() -> None:
    assert _band(0, CUTOFFS) == "None"
    assert _band(1, CUTOFFS) == "Low"


def test_band_boundary_low_to_watch() -> None:
    assert _band(24, CUTOFFS) == "Low"
    assert _band(25, CUTOFFS) == "Watch"


def test_band_boundary_watch_to_high() -> None:
    assert _band(49, CUTOFFS) == "Watch"
    assert _band(50, CUTOFFS) == "High"


def test_band_boundary_high_to_critical() -> None:
    assert _band(74, CUTOFFS) == "High"
    assert _band(75, CUTOFFS) == "Critical"


def test_band_is_always_a_documented_value_across_the_full_range() -> None:
    for score in range(0, 101):
        assert _band(score, CUTOFFS) in RISK_BANDS
