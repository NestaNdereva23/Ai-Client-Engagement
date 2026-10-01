"""Priority tier: a pure lookup, the tier contract, and how it reaches a client.

Covers the withdrawal-cluster boundaries, the seeded tier contract, the
sampling setting's off-by-default behaviour, and that indicator resolution
reads the derived tier only from a rule that defers to it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import delete

from app.db.models.models import ClientFeatures
from app.db.models.rules import TierContract
from app.db.session import SessionLocal
from app.rules.engine import Resolution
from app.rules.indicators import _indicator_dict
from app.rules.tier_contract import (
    TierContractValidationError,
    TierSpec,
    active_tier_contract_version,
    load_active_tiers,
    load_tier,
    save_tier_contract_version,
    validate_tiers,
)
from app.transform.features import (
    DECLINE_LOOKBACK_DAYS,
    FREQUENT_GAP_DAYS,
    LOW_DEPOSITOR_THRESHOLD_KES,
    _withdrawal_cluster,
)

SEEDED_VERSION = 5
IN_FORCE = date(2026, 12, 15)
SEEDED_VALID_FROM = date(2026, 9, 29)


# --- the pure lookup ---


def test_zero_deposits_is_a_hot_lead() -> None:
    assert _withdrawal_cluster(0, []) == "hot_leads"


def test_deposits_at_the_low_depositor_line() -> None:
    assert _withdrawal_cluster(LOW_DEPOSITOR_THRESHOLD_KES, []) == "low_depositors"
    assert _withdrawal_cluster(LOW_DEPOSITOR_THRESHOLD_KES + 1, []) == "one_time_withdrawers"


def test_three_withdrawals_spanning_the_gradual_window() -> None:
    start = date(2024, 1, 1)
    span = start + timedelta(days=DECLINE_LOOKBACK_DAYS)
    dates = [start, start + timedelta(days=90), span]
    assert _withdrawal_cluster(50_000, dates) == "gradual_withdrawers"

    short_dates = [start, start + timedelta(days=90), span - timedelta(days=1)]
    assert _withdrawal_cluster(50_000, short_dates) == "one_time_withdrawers"


def test_two_withdrawals_at_the_frequent_gap() -> None:
    start = date(2024, 1, 1)
    tight = [start, start + timedelta(days=FREQUENT_GAP_DAYS)]
    assert _withdrawal_cluster(50_000, tight) == "frequent_withdrawers"

    loose = [start, start + timedelta(days=FREQUENT_GAP_DAYS + 1)]
    assert _withdrawal_cluster(50_000, loose) == "one_time_withdrawers"


def test_gradual_wins_over_frequent_when_both_would_match() -> None:
    start = date(2024, 1, 1)
    dates = [
        start,
        start + timedelta(days=30),
        start + timedelta(days=60),
        start + timedelta(days=200),
    ]
    assert _withdrawal_cluster(50_000, dates) == "gradual_withdrawers"


# --- the tier contract store ---


def _spec(tier: str = "hot_leads", **overrides) -> TierSpec:
    fields = {
        "tier": tier,
        "display_name": "A tier",
        "primary_channel": "email",
        "max_words": 100,
        "sign_off": "a person",
        "human_approval": True,
        "review_sample_rate": 1.0,
    }
    fields.update(overrides)
    return TierSpec(**fields)


def test_a_contract_may_not_be_empty() -> None:
    with pytest.raises(TierContractValidationError, match="may not be empty"):
        validate_tiers([])


def test_tier_identifiers_must_be_unique() -> None:
    with pytest.raises(TierContractValidationError, match="unique"):
        validate_tiers([_spec("hot_leads"), _spec("hot_leads")])


def test_an_unknown_tier_identifier_is_rejected() -> None:
    with pytest.raises(TierContractValidationError, match="unknown tier"):
        validate_tiers([_spec("T9")])


def test_a_non_positive_word_cap_is_rejected() -> None:
    with pytest.raises(TierContractValidationError, match="word cap"):
        validate_tiers([_spec(max_words=0)])


def test_a_sample_rate_outside_zero_to_one_is_rejected() -> None:
    with pytest.raises(TierContractValidationError, match="sample_rate"):
        validate_tiers([_spec(review_sample_rate=1.5)])


def test_a_well_formed_contract_passes() -> None:
    validate_tiers([_spec("hot_leads"), _spec("low_depositors")])


def test_the_seed_ships_all_five_tiers(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    assert set(tiers) == {
        "hot_leads",
        "low_depositors",
        "gradual_withdrawers",
        "frequent_withdrawers",
        "one_time_withdrawers",
    }


def test_every_tier_uses_email_and_sms(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    assert all(row.primary_channel == "email" for row in tiers.values())
    assert all(row.secondary_channel == "sms" for row in tiers.values())


def test_the_word_caps_match_the_tier_contract(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    assert {tier: row.max_words for tier, row in tiers.items()} == {
        "gradual_withdrawers": 130,
        "frequent_withdrawers": 130,
        "one_time_withdrawers": 120,
        "low_depositors": 90,
        "hot_leads": 90,
    }


def test_every_tier_requires_approval_in_the_contract_itself(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    assert all(row.human_approval for row in tiers.values())


def test_the_earlier_versions_no_longer_come_back_into_force(db: None) -> None:
    with SessionLocal() as session:
        assert active_tier_contract_version(session, SEEDED_VALID_FROM) == SEEDED_VERSION
        assert active_tier_contract_version(session, IN_FORCE) == SEEDED_VERSION


@pytest.fixture
def contract_versions():
    versions: list[int] = []
    yield versions
    if not versions:
        return
    with SessionLocal() as session:
        session.execute(delete(TierContract).where(TierContract.version.in_(versions)))
        session.commit()


def test_a_shipped_version_may_not_be_edited(db: None) -> None:
    with SessionLocal() as session, pytest.raises(TierContractValidationError, match="immutable"):
        save_tier_contract_version(session, SEEDED_VERSION, [_spec()], valid_from=date(2027, 1, 1))


def test_a_later_version_supersedes_the_one_before(db: None, contract_versions) -> None:
    contract_versions.append(77)
    later = date(2027, 2, 1)
    with SessionLocal() as session:
        save_tier_contract_version(session, 77, [_spec("hot_leads")], valid_from=later)
        session.commit()

    with SessionLocal() as session:
        assert active_tier_contract_version(session, later) == 77
        assert active_tier_contract_version(session, IN_FORCE) == SEEDED_VERSION


def test_an_unknown_tier_resolves_to_nothing(db: None) -> None:
    with SessionLocal() as session:
        assert load_tier(session, "T9", IN_FORCE) is None


# --- indicator resolution ---


def _resolution(*, priority_tier: str, urgency: str) -> Resolution:
    return Resolution(
        message_angle="pick_up_again",
        urgency=urgency,
        priority_tier=priority_tier,
        prompt_variant="pick_up_again",
        rule_id=1,
        rule_name="test_rule",
        version=1,
    )


def test_a_p_tier_resolution_keeps_the_rules_own_tier_and_urgency() -> None:
    feature = ClientFeatures(client_id=1, priority_tier="hot_leads")
    row = _indicator_dict(feature, _resolution(priority_tier="P1", urgency="high"))
    assert row["priority_tier"] == "P1"
    assert row["urgency"] == "high"


def test_a_derived_tier_resolution_reads_the_feature_rows_own_tier() -> None:
    feature = ClientFeatures(client_id=1, priority_tier="hot_leads")
    row = _indicator_dict(feature, _resolution(priority_tier="one_time_withdrawers", urgency="low"))
    assert row["priority_tier"] == "hot_leads"
    assert row["urgency"] == "low"


@pytest.mark.parametrize(
    "tier",
    [
        "hot_leads",
        "low_depositors",
        "gradual_withdrawers",
        "frequent_withdrawers",
        "one_time_withdrawers",
    ],
)
def test_every_derived_tier_carries_its_own_urgency(tier: str) -> None:
    feature = ClientFeatures(client_id=1, priority_tier=tier)
    row = _indicator_dict(feature, _resolution(priority_tier="one_time_withdrawers", urgency="low"))
    assert row["priority_tier"] == tier
