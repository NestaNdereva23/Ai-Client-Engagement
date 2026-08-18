"""Read and write the versioned risk config, with validation.

A config ships as a numbered version with a validity window and is never
mutated afterwards. Retuning a weight or a threshold means saving a new
version, so a score computed last month can still be explained against the
exact constants that produced it. Selection mirrors rules/catalog.py: the
active version is the one with the latest valid_from that has started and
not ended.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models.risk import RiskConfigVersion
from app.risk.signals import SIGNAL_FUNCS

# Every threshold a signal or a downstream stage reads by name; a config
# missing one of these can't be saved.
_REQUIRED_THRESHOLDS = (
    "DORMANT_DAYS",
    "HEAVY_WITHDRAWAL_PCT",
    "OVERDUE_MULTIPLE",
    "SHRINKING_TREND",
    "TINY_BALANCE",
    "WORTH_A_CALL_BALANCE",
    "MONTHS_UNTIL_EMPTY",
    "FEE_PER_MONTH",
    "SYSTEM_FEE_MAX",
    "RISK_BAND_CUTOFFS",
)


class RiskConfigValidationError(ValueError):
    """A risk config failed validation and was not written."""


def validate_config(weights: dict[str, float], thresholds: dict[str, float]) -> None:
    """Raise RiskConfigValidationError unless the config is well formed."""
    missing_weights = sorted(set(SIGNAL_FUNCS) - set(weights))
    if missing_weights:
        raise RiskConfigValidationError(f"missing weights for signals: {missing_weights}")
    extra_weights = sorted(set(weights) - set(SIGNAL_FUNCS))
    if extra_weights:
        raise RiskConfigValidationError(f"weights name unknown signals: {extra_weights}")
    total = sum(weights.values())
    if total != 100:
        raise RiskConfigValidationError(f"weights must sum to 100, got {total}")

    missing_thresholds = sorted(set(_REQUIRED_THRESHOLDS) - set(thresholds))
    if missing_thresholds:
        raise RiskConfigValidationError(f"missing thresholds: {missing_thresholds}")

    cutoffs = thresholds.get("RISK_BAND_CUTOFFS")
    if not (isinstance(cutoffs, list | tuple) and len(cutoffs) == 4):
        raise RiskConfigValidationError("RISK_BAND_CUTOFFS must be four cutoffs, one per band")
    if list(cutoffs) != sorted(cutoffs) or len(set(cutoffs)) != 4:
        raise RiskConfigValidationError("RISK_BAND_CUTOFFS must be four strictly ascending numbers")


def save_config_version(
    session: Session,
    version: int,
    weights: dict[str, float],
    thresholds: dict[str, float],
    *,
    fa_call_capacity: int,
    at_risk_min: int,
    digest_cap_per_group: int = 12,
    valid_from: date,
    valid_to: date | None = None,
) -> RiskConfigVersion:
    """Validate and insert a new config version.

    Refuses to touch a version that already exists, so a shipped config is
    never edited underneath a score that already cited it. digest_cap_per_group
    is a rendering knob for the morning digest, not a scoring input, so it
    defaults to the notebook's own cap of 12 rather than being required.
    """
    validate_config(weights, thresholds)

    if session.scalar(select(func.count()).where(RiskConfigVersion.version == version)):
        raise RiskConfigValidationError(f"version {version} already exists and may not be mutated")

    row = RiskConfigVersion(
        version=version,
        weights=weights,
        thresholds=thresholds,
        fa_call_capacity=fa_call_capacity,
        at_risk_min=at_risk_min,
        digest_cap_per_group=digest_cap_per_group,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    session.add(row)
    session.flush()
    return row


def active_config_version(session: Session, at: date) -> int | None:
    """The config version in force on `at`, or None if there is none."""
    return session.scalar(
        select(RiskConfigVersion.version)
        .where(
            RiskConfigVersion.valid_from <= at,
            or_(RiskConfigVersion.valid_to.is_(None), RiskConfigVersion.valid_to > at),
        )
        .order_by(RiskConfigVersion.valid_from.desc(), RiskConfigVersion.version.desc())
        .limit(1)
    )


def load_active_config(session: Session, at: date) -> RiskConfigVersion | None:
    """The full config row in force on `at`, or None if there is none."""
    version = active_config_version(session, at)
    if version is None:
        return None
    return session.scalar(select(RiskConfigVersion).where(RiskConfigVersion.version == version))
