"""The seeded risk configs: re-proves what the migrations proved on write,
against what is actually stored."""

from __future__ import annotations

from datetime import date

from app.db.session import SessionLocal
from app.risk.signals import SIGNAL_FUNCS
from app.risk.store import active_config_version, load_active_config

V1_VERSION = 1
IN_FORCE = date(2026, 8, 11)
V2_VERSION = 2
V2_IN_FORCE = date(2026, 10, 13)


def test_v1_is_the_active_version(db) -> None:
    with SessionLocal() as session:
        assert active_config_version(session, IN_FORCE) == V1_VERSION


def test_v1_weights_name_every_signal_and_sum_to_100(db) -> None:
    with SessionLocal() as session:
        config = load_active_config(session, IN_FORCE)
    assert config is not None
    assert set(config.weights) == set(SIGNAL_FUNCS)
    assert sum(config.weights.values()) == 100


def test_v1_currency_thresholds_are_kes_not_usd(db) -> None:
    """TINY_BALANCE, WORTH_A_CALL_BALANCE and SYSTEM_FEE_MAX are KES figures,
    none needing conversion."""
    with SessionLocal() as session:
        config = load_active_config(session, IN_FORCE)
    assert config is not None
    assert config.thresholds["TINY_BALANCE"] == 100
    assert config.thresholds["SYSTEM_FEE_MAX"] == 100
    assert config.thresholds["WORTH_A_CALL_BALANCE"] == 10_000
    assert config.thresholds["FEE_PER_MONTH"] == 50


def test_v1_capacity_and_gate_match_the_notebook(db) -> None:
    with SessionLocal() as session:
        config = load_active_config(session, IN_FORCE)
    assert config is not None
    assert config.fa_call_capacity == 150
    assert config.at_risk_min == 25


def test_v1_digest_cap_matches_the_notebook(db) -> None:
    with SessionLocal() as session:
        config = load_active_config(session, IN_FORCE)
    assert config is not None
    assert config.digest_cap_per_group == 12


def test_v2_moves_only_the_withdrawal_line_to_the_monthly_fee(db) -> None:
    with SessionLocal() as session:
        before = load_active_config(session, IN_FORCE)
        after = load_active_config(session, V2_IN_FORCE)
    assert (before.version, after.version) == (V1_VERSION, V2_VERSION)
    assert after.thresholds["SYSTEM_FEE_MAX"] == after.thresholds["FEE_PER_MONTH"] == 50
    assert {**before.thresholds, "SYSTEM_FEE_MAX": 50} == after.thresholds
    assert after.weights == before.weights
    assert after.fa_call_capacity == before.fa_call_capacity
    assert after.at_risk_min == before.at_risk_min


def test_v1_band_cutoffs_match_the_notebooks_pd_cut(db) -> None:
    """The notebook bins with pd.cut(risk_score, [-1, 0, 24, 49, 74, 100]);
    the four interior edges are what's stored."""
    with SessionLocal() as session:
        config = load_active_config(session, IN_FORCE)
    assert config is not None
    assert config.thresholds["RISK_BAND_CUTOFFS"] == [0, 24, 49, 74]
