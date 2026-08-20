"""Review sampling for single-generated messages, grouped by campaign x tier.

A cohort is one campaign's worth of single-generated messages for one
client tier. Rather than every message landing in the review queue, a share
of them are marked as samples (assign_cohort_slot); the rest are drafted
normally but stay out of the default queue view (app.services.review
list_pending_messages' is_sample filter). Once every sample is approved,
app.services.review.decide flips the cohort to ready_to_approve_rest, and
approve_cohort_remainder there applies the same outcome to what's left.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.outreach import ReviewCohort
from app.rules.tier_contract import cohort_sample_rate_for, load_tier


@dataclass(frozen=True)
class CohortSlot:
    """Where one message lands relative to sampling: the cohort it belongs
    to (None for a message drafted outside any cohort), and whether it is
    one of that cohort's samples.
    """

    cohort_id: str | None
    is_sample: bool


def get_or_create_cohort(session: Session, *, campaign_id: int, priority_tier: str) -> ReviewCohort:
    """This campaign x tier's cohort, creating it on first use.

    The sample rate and cap are resolved once, from the tier contract in
    force today and the current settings, at creation time -- a later
    contract change does not retroactively resize a cohort already
    sampling.
    """
    existing = session.scalar(
        select(ReviewCohort).where(
            ReviewCohort.campaign_id == campaign_id, ReviewCohort.priority_tier == priority_tier
        )
    )
    if existing is not None:
        return existing

    settings = get_settings()
    tier = load_tier(session, priority_tier, date.today())
    sample_rate = cohort_sample_rate_for(tier, sampling_enabled=settings.tier_sampling_enabled)

    cohort = ReviewCohort(
        cohort_id=uuid.uuid4().hex,
        campaign_id=campaign_id,
        priority_tier=priority_tier,
        sample_rate=sample_rate,
        sample_cap=None if sample_rate is None else settings.cohort_sample_cap,
    )
    session.add(cohort)
    session.flush()
    return cohort


def is_sample_slot(slot: int, *, rate: float | None, cap: int | None) -> bool:
    """Whether the slot-th message in a cohort is one of its samples.

    A rate of None means the cohort samples everything. Otherwise the
    first message is always a sample, so no cohort ever goes out with
    nobody having read anything, and after that every 1/rate-th one is,
    until cap samples have been taken.

    Spacing the samples out like this, rather than taking the first N,
    means the rate holds however large the cohort turns out to be. The
    cohort's final size isn't known when it is created: messages are
    generated in batches, and more enrollments fall due later.
    """
    if rate is None:
        return True
    if slot < 1:
        return False
    if slot == 1:
        return True
    if rate <= 0:
        return False
    every = max(1, round(1 / rate))
    if (slot - 1) % every != 0:
        return False
    taken_so_far = 1 + (slot - 1) // every
    return cap is None or taken_so_far <= cap


def assign_cohort_slot(session: Session, cohort: ReviewCohort) -> bool:
    """Claim the next slot in this cohort, returning whether it is a sample.

    Atomic increment-and-read (UPDATE ... RETURNING) rather than a
    count-then-compare, so two messages generated for the same cohort at
    once can't both read the same pre-increment count and both claim the
    same slot.
    """
    assigned_count = session.execute(
        update(ReviewCohort)
        .where(ReviewCohort.cohort_id == cohort.cohort_id)
        .values(assigned_count=ReviewCohort.assigned_count + 1)
        .returning(ReviewCohort.assigned_count)
    ).scalar_one()
    cohort.assigned_count = assigned_count
    if cohort.status != "sampling":
        # The cohort's samples were already decided and its remainder
        # already approved, so there is nothing left for this message to
        # ride on. It gets reviewed on its own rather than sitting pending
        # and out of the default queue view forever.
        return True
    return is_sample_slot(assigned_count, rate=cohort.sample_rate, cap=cohort.sample_cap)


def resolve_cohort_slot(
    session: Session, *, campaign_id: int, priority_tier: str | None
) -> CohortSlot:
    """The cohort slot a newly drafted message should be created with.

    priority_tier=None (no tier resolved for this run) opts the message out
    of cohort sampling entirely -- it is created the old way, always
    reviewed, same as before this existed.
    """
    if priority_tier is None:
        return CohortSlot(cohort_id=None, is_sample=False)
    cohort = get_or_create_cohort(session, campaign_id=campaign_id, priority_tier=priority_tier)
    is_sample = assign_cohort_slot(session, cohort)
    return CohortSlot(cohort_id=cohort.cohort_id, is_sample=is_sample)
