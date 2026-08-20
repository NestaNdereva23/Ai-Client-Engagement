"""Cohort sampling: which slots become samples, and what a new cohort inherits.

The slot rule is pure and tested without a database. Cohort creation reads
the tier contract in force today, so those tests need one.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete

from app.campaigns.cohorts import (
    assign_cohort_slot,
    get_or_create_cohort,
    is_sample_slot,
    resolve_cohort_slot,
)
from app.config import get_settings
from app.db.models.outreach import Campaign, ReviewCohort
from app.db.session import SessionLocal
from app.rules.tier_contract import cohort_sample_rate_for, load_tier

TODAY_TIER_RATES = {"T1": 0.05, "T2": 0.03, "T3": 0.02, "T4": 0.01}


def _samples_in(cohort_size: int, *, rate: float | None, cap: int | None) -> list[int]:
    return [slot for slot in range(1, cohort_size + 1) if is_sample_slot(slot, rate=rate, cap=cap)]


# --- the slot rule ---


def test_no_rate_means_every_message_is_a_sample() -> None:
    assert _samples_in(5, rate=None, cap=25) == [1, 2, 3, 4, 5]


def test_the_first_message_is_always_a_sample() -> None:
    for rate in (0.0, 0.01, 0.05, 1.0):
        assert is_sample_slot(1, rate=rate, cap=25) is True


def test_a_rate_spaces_the_samples_out_evenly() -> None:
    assert _samples_in(41, rate=0.05, cap=None) == [1, 21, 41]


def test_the_rate_holds_however_large_the_cohort_grows() -> None:
    # The point of spacing rather than taking the first N: the share is the
    # same at 200 messages as at 2,000, without knowing either up front.
    assert len(_samples_in(200, rate=0.05, cap=None)) == pytest.approx(10, abs=1)
    assert len(_samples_in(2000, rate=0.05, cap=None)) == pytest.approx(100, abs=1)


def test_the_cap_stops_a_big_cohort_flooding_the_queue() -> None:
    # 1,700 enrollments at 5 percent would be 85 items for one reviewer.
    assert len(_samples_in(1700, rate=0.05, cap=25)) == 25


def test_a_zero_rate_still_samples_the_first_message_and_no_others() -> None:
    assert _samples_in(500, rate=0.0, cap=25) == [1]


def test_a_rate_of_one_samples_everything() -> None:
    assert _samples_in(4, rate=1.0, cap=25) == [1, 2, 3, 4]


# --- what a cohort inherits at creation ---


def test_the_seeded_contract_carries_a_rate_for_every_tier(db: None) -> None:
    with SessionLocal() as session:
        for tier_name, expected in TODAY_TIER_RATES.items():
            tier = load_tier(session, tier_name, date.today())
            assert tier is not None, f"no contract in force today for {tier_name}"
            assert cohort_sample_rate_for(tier, sampling_enabled=True) == expected


def test_sampling_off_falls_back_to_reviewing_everything(db: None) -> None:
    with SessionLocal() as session:
        tier = load_tier(session, "T1", date.today())
    assert cohort_sample_rate_for(tier, sampling_enabled=False) is None


@pytest.fixture
def campaign_id() -> int:
    with SessionLocal() as session:
        campaign = Campaign(name="cohort sampling test campaign")
        session.add(campaign)
        session.commit()
        created = campaign.campaign_id

    yield created

    with SessionLocal() as session:
        session.execute(delete(Campaign).where(Campaign.campaign_id == created))
        session.commit()


def test_a_new_cohort_takes_the_tiers_rate_and_the_configured_cap(
    db: None, campaign_id: int
) -> None:
    with SessionLocal() as session:
        cohort = get_or_create_cohort(session, campaign_id=campaign_id, priority_tier="T1")
        session.commit()
        assert cohort.sample_rate == TODAY_TIER_RATES["T1"]
        assert cohort.sample_cap == get_settings().cohort_sample_cap


def test_the_same_campaign_and_tier_reuse_one_cohort(db: None, campaign_id: int) -> None:
    with SessionLocal() as session:
        first = get_or_create_cohort(session, campaign_id=campaign_id, priority_tier="T2")
        session.commit()
        second = get_or_create_cohort(session, campaign_id=campaign_id, priority_tier="T2")
        session.commit()
    assert first.cohort_id == second.cohort_id


def test_assigning_slots_marks_only_the_sampled_ones(db: None, campaign_id: int) -> None:
    with SessionLocal() as session:
        cohort = get_or_create_cohort(session, campaign_id=campaign_id, priority_tier="T1")
        session.commit()
        flags = [assign_cohort_slot(session, cohort) for _ in range(21)]
        session.commit()
        assert cohort.assigned_count == 21

    # T1 at 5 percent: the first message, then every twentieth.
    assert [slot for slot, sampled in enumerate(flags, start=1) if sampled] == [1, 21]


def test_a_message_arriving_after_the_cohort_closed_is_reviewed_on_its_own(
    db: None, campaign_id: int
) -> None:
    with SessionLocal() as session:
        cohort = get_or_create_cohort(session, campaign_id=campaign_id, priority_tier="T3")
        assign_cohort_slot(session, cohort)
        cohort.status = "completed"
        session.commit()

        # Slot 2 would not be a sample at T3's rate, but its cohort has
        # already been approved and closed, so it cannot ride on it.
        assert assign_cohort_slot(session, cohort) is True
        session.commit()


def test_a_run_with_no_tier_stays_out_of_cohort_sampling(db: None, campaign_id: int) -> None:
    with SessionLocal() as session:
        slot = resolve_cohort_slot(session, campaign_id=campaign_id, priority_tier=None)
        session.commit()
        assert slot.cohort_id is None
        assert slot.is_sample is False
        assert session.query(ReviewCohort).filter_by(campaign_id=campaign_id).count() == 0
