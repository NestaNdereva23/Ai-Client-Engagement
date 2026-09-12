from __future__ import annotations

import pytest

from app.agents.email_agent import ALLOWED_PLACEHOLDERS, PLACEHOLDER_FACT_FIELDS, placeholder_token
from app.db.session import SessionLocal
from app.personalization import eligibility

POLICY_VERSION = 1


def _expected(angle: str | None) -> dict[str, str]:
    expected = {field: "prohibited" for field in eligibility.FACT_FIELDS}
    for field in eligibility.PLACEHOLDER_FIELDS:
        expected[field] = "placeholder"
    expected["fund_name"] = "direct"
    if angle == eligibility.FEE_WARNING_ANGLE:
        expected["month_the_account_empties"] = "conditional"
    return expected


def test_every_field_resolves_to_exactly_one_mode(db: None) -> None:
    with SessionLocal() as session:
        resolved = eligibility.resolve_fact_eligibility(session, "not_a_goodbye", POLICY_VERSION)

    assert set(resolved) == set(eligibility.FACT_FIELDS)
    for mode in resolved.values():
        assert mode in eligibility.EXPOSURE_MODES


def test_save_fact_eligibility_rule_rejects_a_field_outside_the_schema(db: None) -> None:
    with SessionLocal() as session, pytest.raises(eligibility.FactEligibilityError):
        eligibility.save_fact_eligibility_rule(session, 999999, "not_a_real_field", "direct")


def test_save_fact_eligibility_rule_rejects_an_unknown_exposure_mode(db: None) -> None:
    with SessionLocal() as session, pytest.raises(eligibility.FactEligibilityError):
        eligibility.save_fact_eligibility_rule(session, 999999, "fund_name", "loud")


@pytest.mark.parametrize("angle", ["fee_warning", "not_a_goodbye", "sitting_still"])
def test_v1_resolved_output_matches_todays_hardcoded_behavior(db: None, angle: str) -> None:
    with SessionLocal() as session:
        resolved = eligibility.resolve_fact_eligibility(session, angle, POLICY_VERSION)

    assert resolved == _expected(angle)


def test_filter_facts_for_prompt_splits_direct_placeholder_and_drops_prohibited(
    db: None,
) -> None:
    with SessionLocal() as session:
        resolved = eligibility.resolve_fact_eligibility(
            session, eligibility.FEE_WARNING_ANGLE, POLICY_VERSION
        )

    facts = {
        "typical_contribution_kes": 45000,
        "fund_name": "Cytonn Money Market Fund",
        "month_the_account_empties": "December 2026",
        "recency_band": "recent",
    }
    filtered = eligibility.filter_facts_for_prompt(facts, resolved)

    assert filtered.direct == {
        "fund_name": "Cytonn Money Market Fund",
        "month_the_account_empties": "December 2026",
    }
    assert filtered.placeholder == {"typical_contribution_kes": "typical_contribution"}


def test_filter_facts_for_prompt_drops_a_conditional_field_off_its_angle(db: None) -> None:
    with SessionLocal() as session:
        resolved = eligibility.resolve_fact_eligibility(session, "not_a_goodbye", POLICY_VERSION)

    facts = {"month_the_account_empties": "December 2026"}
    filtered = eligibility.filter_facts_for_prompt(facts, resolved)

    assert filtered.direct == {}
    assert filtered.placeholder == {}


def test_email_agent_placeholder_helpers_still_work() -> None:
    assert PLACEHOLDER_FACT_FIELDS == eligibility.PLACEHOLDER_FACT_FIELDS
    assert placeholder_token("typical_contribution") == "{{typical_contribution}}"
    assert "{{cadence_interval_days}}" in ALLOWED_PLACEHOLDERS
    with pytest.raises(ValueError):
        placeholder_token("typical_contribution_kes")
