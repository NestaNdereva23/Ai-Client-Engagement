"""Priority tier: a pure lookup, the tier contract, and how it reaches a client.

Covers the sixteen band combinations, the seeded tier contract, the sampling
setting's off-by-default behaviour, and that indicator resolution reads the
derived tier only from a rule that defers to it.
"""

from __future__ import annotations

from datetime import date

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
    instance_needs_review,
    load_active_tiers,
    load_tier,
    mandatory_review,
    save_tier_contract_version,
    validate_tiers,
)
from app.transform.features import _priority_tier

SEEDED_VERSION = 1
IN_FORCE = date(2026, 12, 15)

VALUE_BANDS = ("Low", "Medium", "High", "Top")
RECENCY_BANDS = ("Over 6y", "3 to 6y", "1 to 3y", "Under 1y")

# An independent computation of the same score and cut, not a call into the
# function under test, so this actually checks the sixteen combinations rather
# than confirming the code agrees with itself.
_VALUE_POINTS = {"Low": 0, "Medium": 1, "High": 2, "Top": 3}
_RECENCY_POINTS = {"Over 6y": 0, "3 to 6y": 1, "1 to 3y": 2, "Under 1y": 3}


def _expected_tier(value_band: str, recency_band: str) -> str:
    score = _VALUE_POINTS[value_band] * 2 + _RECENCY_POINTS[recency_band]
    if score <= 2:
        return "T4"
    if score <= 4:
        return "T3"
    if score <= 6:
        return "T2"
    return "T1"


# --- the pure lookup ---


@pytest.mark.parametrize(
    ("value_band", "recency_band"), [(v, r) for v in VALUE_BANDS for r in RECENCY_BANDS]
)
def test_every_band_combination_matches_an_independent_computation(
    value_band, recency_band
) -> None:
    assert _priority_tier(value_band, recency_band) == _expected_tier(value_band, recency_band)


def test_the_score_boundaries_land_on_the_stated_cuts() -> None:
    # A client scoring exactly 2, 4, or 6 belongs to the lower tier.
    assert _priority_tier("Medium", "Over 6y") == "T4"  # score 2
    assert _priority_tier("High", "Over 6y") == "T3"  # score 4
    assert _priority_tier("Top", "Over 6y") == "T2"  # score 6


def test_an_unrecognised_recency_band_is_treated_as_unknown() -> None:
    assert _priority_tier("Top", "Unknown") == _priority_tier("Top", "Over 6y")


# --- the tier contract store ---


def _spec(tier: str = "T1", **overrides) -> TierSpec:
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
        validate_tiers([_spec("T1"), _spec("T1")])


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
    validate_tiers([_spec("T1"), _spec("T2")])


def test_the_seed_ships_all_four_tiers(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    assert set(tiers) == {"T1", "T2", "T3", "T4"}


def test_every_tier_uses_email_as_its_primary_channel(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    assert all(row.primary_channel == "email" for row in tiers.values())


def test_the_word_caps_match_the_tier_contract(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    assert {tier: row.max_words for tier, row in tiers.items()} == {
        "T1": 120,
        "T2": 140,
        "T3": 110,
        "T4": 60,
    }


def test_only_the_top_tier_carries_a_call_brief(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    assert tiers["T1"].secondary_channel == "call_brief"
    assert tiers["T2"].secondary_channel is None
    assert tiers["T3"].secondary_channel is None


def test_only_the_top_tier_requires_approval_in_the_contract_itself(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    assert [tier for tier, row in tiers.items() if row.human_approval] == ["T1"]


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
        save_tier_contract_version(session, 77, [_spec("T1")], valid_from=later)
        session.commit()

    with SessionLocal() as session:
        assert active_tier_contract_version(session, later) == 77
        assert active_tier_contract_version(session, IN_FORCE) == SEEDED_VERSION


def test_an_unknown_tier_resolves_to_nothing(db: None) -> None:
    with SessionLocal() as session:
        assert load_tier(session, "T9", IN_FORCE) is None


# --- sampling stays off by default ---


def test_review_is_mandatory_for_every_tier_when_sampling_is_off(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    for row in tiers.values():
        assert mandatory_review(row, sampling_enabled=False) is True


def test_sampling_on_defers_to_the_tiers_own_policy(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    assert mandatory_review(tiers["T1"], sampling_enabled=True) is True
    assert mandatory_review(tiers["T2"], sampling_enabled=True) is False
    assert mandatory_review(tiers["T4"], sampling_enabled=True) is False


# --- instance-level sampling ---


def test_instance_needs_review_is_always_true_with_no_tier_row_at_all() -> None:
    assert instance_needs_review(None, sampling_enabled=True) is True
    assert instance_needs_review(None, sampling_enabled=False) is True


def test_instance_needs_review_is_always_true_with_sampling_off(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    for row in tiers.values():
        assert instance_needs_review(row, sampling_enabled=False) is True


def test_instance_needs_review_is_always_true_for_a_mandatory_tier(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    assert instance_needs_review(tiers["T1"], sampling_enabled=True) is True


def test_instance_needs_review_follows_the_tiers_own_rate_once_not_mandatory(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    t3 = tiers["T3"]
    assert 0.0 < t3.review_sample_rate < 1.0

    monkeypatch.setattr("app.rules.tier_contract.random.random", lambda: t3.review_sample_rate / 2)
    assert instance_needs_review(t3, sampling_enabled=True) is True

    monkeypatch.setattr(
        "app.rules.tier_contract.random.random", lambda: min(t3.review_sample_rate * 2, 0.999999)
    )
    assert instance_needs_review(t3, sampling_enabled=True) is False


def test_instance_needs_review_is_never_true_for_a_zero_rate_tier(db: None) -> None:
    with SessionLocal() as session:
        tiers = load_active_tiers(session, IN_FORCE)
    t4 = tiers["T4"]
    assert t4.review_sample_rate == 0.0
    assert instance_needs_review(t4, sampling_enabled=True) is False


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
    feature = ClientFeatures(client_id=1, priority_tier="T1")
    row = _indicator_dict(feature, _resolution(priority_tier="P1", urgency="high"))
    assert row["priority_tier"] == "P1"
    assert row["urgency"] == "high"


def test_a_derived_tier_resolution_reads_the_feature_rows_own_tier() -> None:
    feature = ClientFeatures(client_id=1, priority_tier="T1")
    # The rule's own urgency ("low") must be discarded in favour of T1's own.
    row = _indicator_dict(feature, _resolution(priority_tier="T3", urgency="low"))
    assert row["priority_tier"] == "T1"
    assert row["urgency"] == "high"


@pytest.mark.parametrize("tier", ["T1", "T2", "T3", "T4"])
def test_every_derived_tier_carries_its_own_urgency(tier: str) -> None:
    feature = ClientFeatures(client_id=1, priority_tier=tier)
    row = _indicator_dict(feature, _resolution(priority_tier="T3", urgency="low"))
    assert row["priority_tier"] == tier
