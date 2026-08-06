"""Validation and versioning for the business-rule store.

The pure tests exercise validate_rules; the database tests prove a version is
written once, never mutated, and that a later version supersedes an earlier
one at the right date.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete

from app.db.models.rules import BusinessRule
from app.db.session import SessionLocal
from app.rules.store import (
    RuleSpec,
    RuleValidationError,
    load_active_rules,
    save_version,
    validate_rules,
)


def _good_rules() -> list[RuleSpec]:
    return [
        RuleSpec(
            name="high_value",
            priority=10,
            match={"value_band": ["Top"]},
            message_angle="back_on_schedule",
            urgency="high",
            priority_tier="T1",
            prompt_variant="back_on_schedule",
        ),
        RuleSpec(name="catch_all", priority=20),
    ]


def test_a_well_formed_rule_set_validates() -> None:
    assert validate_rules(_good_rules()) is None


def test_unknown_match_field_is_rejected() -> None:
    rules = [RuleSpec(name="bad", priority=10, match={"client_name": ["Jane"]})]
    with pytest.raises(RuleValidationError, match="unknown field"):
        validate_rules(rules)


def test_value_outside_a_fields_range_is_rejected() -> None:
    rules = [RuleSpec(name="bad", priority=10, match={"value_band": ["Platinum"]})]
    with pytest.raises(RuleValidationError, match="outside its range"):
        validate_rules(rules)


def test_unknown_output_is_rejected() -> None:
    rules = [RuleSpec(name="bad", priority=10, message_angle="hard_sell")]
    with pytest.raises(RuleValidationError, match="angle"):
        validate_rules(rules)


def test_duplicate_priority_is_rejected() -> None:
    rules = [RuleSpec(name="a", priority=10), RuleSpec(name="b", priority=10)]
    with pytest.raises(RuleValidationError, match="unique"):
        validate_rules(rules)


def test_a_rule_shadowed_by_an_earlier_broader_rule_is_unreachable() -> None:
    rules = [
        RuleSpec(name="broad", priority=10, match={"value_band": ["Top"]}),
        RuleSpec(
            name="narrow",
            priority=20,
            match={"value_band": ["Top"], "recency_band": ["Under 1y"]},
        ),
    ]
    with pytest.raises(RuleValidationError, match="unreachable"):
        validate_rules(rules)


def test_a_wildcard_before_other_rules_makes_them_unreachable() -> None:
    rules = [
        RuleSpec(name="catch_all", priority=10),
        RuleSpec(name="specific", priority=20, match={"value_band": ["Low"]}),
    ]
    with pytest.raises(RuleValidationError, match="unreachable"):
        validate_rules(rules)


def test_specific_before_broad_is_reachable() -> None:
    rules = [
        RuleSpec(
            name="narrow",
            priority=10,
            match={"value_band": ["Top"], "recency_band": ["Under 1y"]},
        ),
        RuleSpec(name="broad", priority=20, match={"value_band": ["Top"]}),
    ]
    assert validate_rules(rules) is None


# --- Database: versioning and the shipped seed ------------------------------


@pytest.fixture
def temp_versions(db: None):
    """Remove any rule versions written by a test, keeping the seed intact."""
    written: list[int] = []
    yield written
    if not written:
        return
    with SessionLocal() as session:
        session.execute(delete(BusinessRule).where(BusinessRule.version.in_(written)))
        session.commit()


def test_save_version_writes_and_refuses_to_mutate_a_shipped_version(temp_versions) -> None:
    temp_versions.append(99)
    with SessionLocal() as session:
        count = save_version(
            session, 99, _good_rules(), valid_from=date(2030, 1, 1), valid_to=date(2031, 1, 1)
        )
        session.commit()
    assert count == 2

    # A second save of the same version must be refused, not mutate it.
    with SessionLocal() as session:
        with pytest.raises(RuleValidationError, match="already exists"):
            save_version(
                session, 99, _good_rules(), valid_from=date(2031, 2, 1), valid_to=date(2032, 1, 1)
            )


def test_a_later_version_supersedes_the_one_before_it(temp_versions) -> None:
    temp_versions.extend([50, 51])
    superseding = [
        RuleSpec(name="only", priority=10, match={"value_band": ["Top"]}),
        RuleSpec(name="catch_all", priority=20),
    ]
    with SessionLocal() as session:
        save_version(session, 50, _good_rules(), valid_from=date(2050, 1, 1))
        save_version(session, 51, superseding, valid_from=date(2050, 6, 1))
        session.commit()

    with SessionLocal() as session:
        before = load_active_rules(session, at=date(2050, 3, 1))
        after = load_active_rules(session, at=date(2050, 9, 1))
    assert {r.version for r in before} == {50}
    assert {r.version for r in after} == {51}
