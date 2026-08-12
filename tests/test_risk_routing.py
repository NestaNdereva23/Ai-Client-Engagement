"""Tests for the five-queue router: precedence, capacity, and the two
overrides (complaint, suppression).

Pure: builds unpersisted RiskConfigVersion rows and RoutableRow fixtures
directly, no database involved.
"""

from __future__ import annotations

from app.db.models.risk import RiskConfigVersion
from app.risk.routing import RoutableRow, call_queue_keys, route_population

THRESHOLDS = {
    "DUST_BALANCE": 100,
    "MATERIAL_BALANCE": 10_000,
}


def _config(fa_call_capacity: int = 150, at_risk_min: int = 25) -> RiskConfigVersion:
    return RiskConfigVersion(
        thresholds=THRESHOLDS, fa_call_capacity=fa_call_capacity, at_risk_min=at_risk_min
    )


def _row(key: tuple[int, int] = (1, 10), **overrides) -> RoutableRow:
    base = dict(
        key=key,
        balance=500_000.0,
        risk_score=50,
        sig_dormant=False,
        aum_at_risk=250_000.0,
    )
    base.update(overrides)
    return RoutableRow(**base)


def _route(row: RoutableRow, config: RiskConfigVersion) -> str:
    return route_population([row], config)[row.key].route


# --- precedence order ---


def test_dust_and_dormant_lands_on_dust_cleanup() -> None:
    row = _row(balance=50.0, sig_dormant=True, risk_score=90, aum_at_risk=45.0)
    assert _route(row, _config()) == "dust_cleanup"


def test_dust_but_not_dormant_does_not_land_on_dust_cleanup() -> None:
    row = _row(balance=50.0, sig_dormant=False, risk_score=90, aum_at_risk=45.0)
    assert _route(row, _config()) != "dust_cleanup"


def test_material_and_at_risk_lands_on_fa_digest_watch_when_below_capacity() -> None:
    # Alone in the population, so it's within capacity, yet not the top by
    # aum_at_risk relative to nothing else -- still routes to digest_watch
    # once a second row with higher aum_at_risk takes the one call slot.
    config = _config(fa_call_capacity=1)
    top = _row((1, 10), aum_at_risk=1_000_000.0)
    second = _row((2, 10), aum_at_risk=500_000.0)
    results = route_population([top, second], config)
    assert results[top.key].route == "fa_call_priority"
    assert results[second.key].route == "fa_digest_watch"


def test_material_and_at_risk_wins_over_automated_nurture_when_not_dust() -> None:
    # material & at_risk overlaps with not-dust & at_risk -- fa_digest_watch
    # is checked first, so it wins even though this row would also satisfy
    # automated_nurture's own condition.
    row = _row(balance=200_000.0, risk_score=50, aum_at_risk=100_000.0)
    assert _route(row, _config(fa_call_capacity=0)) == "fa_digest_watch"


def test_non_dust_at_risk_immaterial_lands_on_automated_nurture() -> None:
    row = _row(balance=5_000.0, risk_score=30, aum_at_risk=1_500.0)
    assert _route(row, _config()) == "automated_nurture"


def test_not_at_risk_and_not_dust_lands_on_monitor_only() -> None:
    row = _row(balance=5_000.0, risk_score=10, aum_at_risk=500.0)
    assert _route(row, _config()) == "monitor_only"


def test_everything_else_lands_on_monitor_only() -> None:
    row = _row(balance=200_000.0, risk_score=0, aum_at_risk=0.0)
    assert _route(row, _config()) == "monitor_only"


# --- capacity-bounded call queue ---


def test_call_queue_cuts_at_exactly_capacity() -> None:
    config = _config(fa_call_capacity=3)
    rows = [_row((i, 10), aum_at_risk=float(100 - i), risk_score=40) for i in range(10)]
    queue = call_queue_keys(rows, config)
    assert len(queue) == 3
    assert queue == {(0, 10), (1, 10), (2, 10)}


def test_call_queue_ignores_immaterial_rows_even_at_a_high_score() -> None:
    config = _config(fa_call_capacity=5)
    row = _row(balance=5_000.0, risk_score=100, aum_at_risk=5_000.0)
    assert call_queue_keys([row], config) == set()


def test_call_queue_ignores_rows_below_at_risk_min() -> None:
    row = _row(risk_score=24, aum_at_risk=1_000_000.0)
    assert call_queue_keys([row], _config(fa_call_capacity=5, at_risk_min=25)) == set()


def test_queue_rank_is_assigned_by_aum_at_risk_descending() -> None:
    config = _config(fa_call_capacity=3)
    rows = [
        _row((1, 10), aum_at_risk=300.0),
        _row((2, 10), aum_at_risk=900.0),
        _row((3, 10), aum_at_risk=600.0),
    ]
    results = route_population(rows, config)
    assert results[(2, 10)].queue_rank == 1
    assert results[(3, 10)].queue_rank == 2
    assert results[(1, 10)].queue_rank == 3


def test_queue_rank_is_none_off_the_call_queue() -> None:
    row = _row(balance=5_000.0, risk_score=30, aum_at_risk=1_500.0)
    result = route_population([row], _config())[row.key]
    assert result.route == "automated_nurture"
    assert result.queue_rank is None


# --- complaint override ---


def test_open_complaint_keeps_a_high_scoring_client_out_of_automated_nurture() -> None:
    row = _row(balance=5_000.0, risk_score=90, aum_at_risk=4_500.0, has_open_complaint=True)
    result = route_population([row], _config())[row.key]
    assert result.route != "automated_nurture"
    assert result.route == "fa_digest_watch"


def test_no_complaint_routes_normally_to_automated_nurture() -> None:
    row = _row(balance=5_000.0, risk_score=90, aum_at_risk=4_500.0, has_open_complaint=False)
    result = route_population([row], _config())[row.key]
    assert result.route == "automated_nurture"


def test_complaint_caveat_is_carried_regardless_of_route() -> None:
    row = _row(balance=200_000.0, risk_score=0, has_open_complaint=True)
    result = route_population([row], _config())[row.key]
    assert result.route == "monitor_only"
    assert result.complaint_caveat is True


def test_complaint_override_does_not_touch_a_dust_cleanup_route() -> None:
    row = _row(balance=50.0, sig_dormant=True, risk_score=90, has_open_complaint=True)
    result = route_population([row], _config())[row.key]
    assert result.route == "dust_cleanup"


# --- suppression override ---


def test_suppressed_client_never_routes_to_automated_nurture() -> None:
    row = _row(balance=5_000.0, risk_score=90, aum_at_risk=4_500.0, suppressed=True)
    result = route_population([row], _config())[row.key]
    assert result.route != "automated_nurture"
    assert result.route == "monitor_only"


def test_unsuppressed_client_routes_normally_to_automated_nurture() -> None:
    row = _row(balance=5_000.0, risk_score=90, aum_at_risk=4_500.0, suppressed=False)
    result = route_population([row], _config())[row.key]
    assert result.route == "automated_nurture"


def test_suppression_does_not_touch_a_dust_cleanup_route() -> None:
    row = _row(balance=50.0, sig_dormant=True, risk_score=90, suppressed=True)
    result = route_population([row], _config())[row.key]
    assert result.route == "dust_cleanup"


def test_complaint_takes_precedence_when_both_flags_are_set() -> None:
    row = _row(
        balance=5_000.0,
        risk_score=90,
        aum_at_risk=4_500.0,
        has_open_complaint=True,
        suppressed=True,
    )
    result = route_population([row], _config())[row.key]
    assert result.route == "fa_digest_watch"
