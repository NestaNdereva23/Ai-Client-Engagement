"""A known control sample must resolve to exactly the expected outcome.

Each representative client is pinned to its rule name, angle, prompt variant, and
priority tier under the active v2 rules, and the winning rule id logged on the
resolution is checked against the stored rule. The censored case locks in the
softening from M4.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models.rules import BusinessRule
from app.db.session import SessionLocal
from app.rules.engine import resolve
from app.rules.store import load_active_rules

# v2 is in force from 2026-08-01; resolve the control sample under it.
AT = date(2026, 8, 15)


@dataclass(frozen=True)
class Case:
    label: str
    features: dict[str, Any]
    rule_name: str
    angle: str
    variant: str
    tier: str


CONTROL_SAMPLE = [
    Case(
        "frequent top value",
        {"archetype": "Frequent (5+, censored)", "value_tier": "Top", "purchases_censored": True},
        "frequent_high_value",
        "winback_habit",
        "habit_premium_soft",
        "P1",
    ),
    Case(
        "frequent mid value",
        {"archetype": "Frequent (5+, censored)", "value_tier": "Mid"},
        "frequent_default",
        "winback_habit",
        "habit_standard_soft",
        "P2",
    ),
    Case(
        "occasional high value, not censored",
        {"archetype": "Occasional (2-4)", "value_tier": "High", "history_censored": False},
        "occasional_high_value",
        "winback_habit",
        "habit_standard",
        "P2",
    ),
    Case(
        # High value, but a full sales window truncates history: soften, do not
        # claim a habit the censored window cannot support.
        "occasional high value, censored history",
        {"archetype": "Occasional (2-4)", "value_tier": "High", "history_censored": True},
        "occasional_censored",
        "winback_flexible",
        "flexible_soft",
        "P3",
    ),
    Case(
        "one and done, low value",
        {"archetype": "One-and-done", "value_tier": "Low"},
        "one_and_done_default",
        "winback_flexible",
        "flexible_standard",
        "P3",
    ),
    Case(
        "one and done, censored history",
        {"archetype": "One-and-done", "value_tier": "Low", "history_censored": True},
        "one_and_done_censored",
        "winback_flexible",
        "flexible_soft",
        "P3",
    ),
    Case(
        "none observed",
        {"archetype": "None observed"},
        "none_observed",
        "winback_flexible",
        "flexible_minimal",
        "P3",
    ),
]


@pytest.fixture
def active_rules(db: None):
    with SessionLocal() as session:
        rules = load_active_rules(session, at=AT)
        ids = {
            r.name: r.rule_id
            for r in session.scalars(select(BusinessRule).where(BusinessRule.version == 2))
        }
    if not rules:
        pytest.skip("v2 rules not seeded; run alembic upgrade head")
    return rules, ids


@pytest.mark.parametrize("case", CONTROL_SAMPLE, ids=lambda c: c.label)
def test_control_sample_resolves_exactly(case: Case, active_rules) -> None:
    rules, rule_ids = active_rules
    result = resolve(case.features, rules)
    assert result.rule_name == case.rule_name
    assert result.message_angle == case.angle
    assert result.prompt_variant == case.variant
    assert result.priority_tier == case.tier
    assert result.version == 2
    # The logged rule id points at the stored rule that won.
    assert result.rule_id == rule_ids[case.rule_name]


def test_censored_history_never_resolves_to_an_exact_count_variant(active_rules) -> None:
    rules, _ = active_rules
    # A censored client must land on a soft variant, never one that would assert
    # exact counts or totals.
    censored = resolve(
        {"archetype": "Occasional (2-4)", "value_tier": "High", "history_censored": True}, rules
    )
    not_censored = resolve(
        {"archetype": "Occasional (2-4)", "value_tier": "High", "history_censored": False}, rules
    )
    assert censored.prompt_variant.endswith("_soft")
    assert not not_censored.prompt_variant.endswith("_soft")
