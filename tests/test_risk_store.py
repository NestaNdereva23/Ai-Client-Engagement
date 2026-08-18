"""Tests for the versioned risk config store: validation and immutability."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete

from app.db.models.risk import RiskConfigVersion
from app.db.session import SessionLocal
from app.risk.store import (
    RiskConfigValidationError,
    active_config_version,
    load_active_config,
    save_config_version,
)

_WEIGHTS = {
    "sig_heavy_withdrawal": 30,
    "sig_dormant": 25,
    "sig_broken_pattern": 20,
    "sig_shrinking": 15,
    "sig_going_dormant": 7,
    "sig_never_repeated": 3,
}
_THRESHOLDS = {
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


@pytest.fixture
def cleanup_versions():
    """Collect config versions to delete after the test."""
    versions: list[int] = []
    yield versions
    with SessionLocal() as session:
        session.execute(delete(RiskConfigVersion).where(RiskConfigVersion.version.in_(versions)))
        session.commit()


def test_weights_must_sum_to_100(db, cleanup_versions) -> None:
    cleanup_versions.append(90001)
    bad_weights = {**_WEIGHTS, "sig_heavy_withdrawal": 31}  # now sums to 101
    with SessionLocal() as session, pytest.raises(RiskConfigValidationError, match="sum to 100"):
        save_config_version(
            session,
            90001,
            bad_weights,
            _THRESHOLDS,
            fa_call_capacity=150,
            at_risk_min=25,
            valid_from=date(2020, 1, 1),
        )


def test_weights_must_name_every_signal(db, cleanup_versions) -> None:
    cleanup_versions.append(90002)
    missing = {k: v for k, v in _WEIGHTS.items() if k != "sig_never_repeated"}
    with (
        SessionLocal() as session,
        pytest.raises(RiskConfigValidationError, match="missing weights"),
    ):
        save_config_version(
            session,
            90002,
            missing,
            _THRESHOLDS,
            fa_call_capacity=150,
            at_risk_min=25,
            valid_from=date(2020, 1, 1),
        )


def test_thresholds_must_be_complete(db, cleanup_versions) -> None:
    cleanup_versions.append(90003)
    missing = {k: v for k, v in _THRESHOLDS.items() if k != "SYSTEM_FEE_MAX"}
    with (
        SessionLocal() as session,
        pytest.raises(RiskConfigValidationError, match="missing thresholds"),
    ):
        save_config_version(
            session,
            90003,
            _WEIGHTS,
            missing,
            fa_call_capacity=150,
            at_risk_min=25,
            valid_from=date(2020, 1, 1),
        )


def test_band_cutoffs_must_have_four_values(db, cleanup_versions) -> None:
    cleanup_versions.append(90007)
    bad = {**_THRESHOLDS, "RISK_BAND_CUTOFFS": [0, 24, 49]}
    with (
        SessionLocal() as session,
        pytest.raises(RiskConfigValidationError, match="four cutoffs"),
    ):
        save_config_version(
            session,
            90007,
            _WEIGHTS,
            bad,
            fa_call_capacity=150,
            at_risk_min=25,
            valid_from=date(2020, 1, 1),
        )


def test_band_cutoffs_must_be_strictly_ascending(db, cleanup_versions) -> None:
    cleanup_versions.append(90008)
    bad = {**_THRESHOLDS, "RISK_BAND_CUTOFFS": [0, 49, 24, 74]}
    with (
        SessionLocal() as session,
        pytest.raises(RiskConfigValidationError, match="ascending"),
    ):
        save_config_version(
            session,
            90008,
            _WEIGHTS,
            bad,
            fa_call_capacity=150,
            at_risk_min=25,
            valid_from=date(2020, 1, 1),
        )


def test_a_version_cannot_be_saved_twice(db, cleanup_versions) -> None:
    cleanup_versions.append(90004)
    with SessionLocal() as session:
        save_config_version(
            session,
            90004,
            _WEIGHTS,
            _THRESHOLDS,
            fa_call_capacity=150,
            at_risk_min=25,
            valid_from=date(2020, 1, 1),
        )
        session.commit()

    with (
        SessionLocal() as session,
        pytest.raises(RiskConfigValidationError, match="already exists"),
    ):
        save_config_version(
            session,
            90004,
            _WEIGHTS,
            _THRESHOLDS,
            fa_call_capacity=150,
            at_risk_min=25,
            valid_from=date(2021, 1, 1),
        )


def test_active_version_picks_the_window_in_force(db, cleanup_versions) -> None:
    cleanup_versions.extend([90005, 90006])
    with SessionLocal() as session:
        save_config_version(
            session,
            90005,
            _WEIGHTS,
            _THRESHOLDS,
            fa_call_capacity=150,
            at_risk_min=25,
            valid_from=date(2020, 1, 1),
            valid_to=date(2021, 1, 1),
        )
        save_config_version(
            session,
            90006,
            _WEIGHTS,
            _THRESHOLDS,
            fa_call_capacity=200,
            at_risk_min=30,
            valid_from=date(2021, 1, 1),
        )
        session.commit()

    with SessionLocal() as session:
        assert active_config_version(session, date(2020, 6, 1)) == 90005
        assert active_config_version(session, date(2021, 6, 1)) == 90006
        config = load_active_config(session, date(2021, 6, 1))
        assert config is not None
        assert config.fa_call_capacity == 200


def test_no_active_version_before_anything_is_seeded(db) -> None:
    with SessionLocal() as session:
        assert active_config_version(session, date(1999, 1, 1)) is None
