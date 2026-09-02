"""Read and write the versioned tier contract, with validation.

A contract ships as a numbered version with a validity window and is never
mutated afterwards, mirroring the angle catalogue. Selection uses the same
latest-valid_from-then-highest-version rule as the rule store and the
catalogue, so all three stay in step when moved together.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models.rules import TierContract
from app.transform.features import PRIORITY_TIERS


class TierContractValidationError(ValueError):
    """A tier contract failed validation and was not written."""


@dataclass(frozen=True)
class TierSpec:
    """One tier's channel, format, and review policy."""

    tier: str
    display_name: str
    primary_channel: str
    max_words: int
    sign_off: str
    human_approval: bool
    review_sample_rate: float
    # The share of a campaign x tier cohort's single-generated messages to
    # sample. None means every message in one of this tier's cohorts must
    # be reviewed -- see cohort_sample_rate_for.
    cohort_sample_rate: float | None = None
    secondary_channel: str | None = None


def validate_tiers(tiers: Sequence[TierSpec]) -> None:
    """Raise TierContractValidationError unless the contract is well formed."""
    if not tiers:
        raise TierContractValidationError("a tier contract may not be empty")

    identifiers = [t.tier for t in tiers]
    if len(set(identifiers)) != len(identifiers):
        raise TierContractValidationError("tier identifiers must be unique")

    unknown = sorted(set(identifiers) - PRIORITY_TIERS)
    if unknown:
        raise TierContractValidationError(f"unknown tier identifiers: {unknown}")

    for spec in tiers:
        if not spec.display_name.strip():
            raise TierContractValidationError(f"tier '{spec.tier}' has no display name")
        if not spec.primary_channel.strip():
            raise TierContractValidationError(f"tier '{spec.tier}' has no primary channel")
        if not spec.sign_off.strip():
            raise TierContractValidationError(f"tier '{spec.tier}' has no sign off")
        if spec.max_words <= 0:
            raise TierContractValidationError(f"tier '{spec.tier}' has a non-positive word cap")
        if not 0.0 <= spec.review_sample_rate <= 1.0:
            raise TierContractValidationError(f"tier '{spec.tier}' review_sample_rate out of range")
        if spec.cohort_sample_rate is not None and not 0.0 <= spec.cohort_sample_rate <= 1.0:
            raise TierContractValidationError(f"tier '{spec.tier}' cohort_sample_rate out of range")


def save_tier_contract_version(
    session: Session,
    version: int,
    tiers: Sequence[TierSpec],
    *,
    valid_from: date,
    valid_to: date | None = None,
) -> int:
    """Validate and insert a new tier contract version, returning the row count."""
    validate_tiers(tiers)

    if session.scalar(select(func.count()).where(TierContract.version == version)):
        raise TierContractValidationError(f"version {version} already exists and is immutable")

    session.add_all(
        TierContract(
            version=version,
            tier=spec.tier,
            display_name=spec.display_name,
            primary_channel=spec.primary_channel,
            secondary_channel=spec.secondary_channel,
            max_words=spec.max_words,
            sign_off=spec.sign_off,
            human_approval=spec.human_approval,
            review_sample_rate=spec.review_sample_rate,
            cohort_sample_rate=spec.cohort_sample_rate,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        for spec in tiers
    )
    session.flush()
    return len(tiers)


def active_tier_contract_version(session: Session, at: date) -> int | None:
    """The tier contract version in force on `at`, or None if there is none."""
    return session.scalar(
        select(TierContract.version)
        .where(
            TierContract.valid_from <= at,
            or_(TierContract.valid_to.is_(None), TierContract.valid_to > at),
        )
        .order_by(TierContract.valid_from.desc(), TierContract.version.desc())
        .limit(1)
    )


def load_active_tiers(session: Session, at: date) -> dict[str, TierContract]:
    """The active tier contract for `at`, keyed by tier identifier."""
    version = active_tier_contract_version(session, at)
    if version is None:
        return {}
    rows = session.scalars(
        select(TierContract).where(TierContract.version == version).order_by(TierContract.tier)
    ).all()
    return {row.tier: row for row in rows}


def load_tier(session: Session, tier: str, at: date) -> TierContract | None:
    """One tier's contract from the version in force on `at`."""
    return load_active_tiers(session, at).get(tier)


def cohort_sample_rate_for(tier: TierContract | None, *, sampling_enabled: bool) -> float | None:
    if tier is None or not sampling_enabled:
        return None
    return tier.cohort_sample_rate
