"""First-match-wins resolution over the business rules.

The pure tests build rules in memory to check ordering, the wildcard fallback,
boolean matching, and that only allow-listed features are read. One database
test resolves representative clients against the seeded v1 set.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.db.models.models import ClientFeatures
from app.db.models.rules import BusinessRule
from app.db.session import SessionLocal
from app.rules.engine import NoRuleMatched, Resolution, feature_view, resolve
from app.rules.store import load_active_rules


def _rule(priority: int, match: dict[str, Any], **outputs: str) -> BusinessRule:
    base = {
        "message_angle": "winback_flexible",
        "urgency": "low",
        "priority_tier": "P3",
        "prompt_variant": "flexible_standard",
    }
    base.update(outputs)
    return BusinessRule(
        rule_id=priority,
        version=1,
        priority=priority,
        name=f"rule_{priority}",
        match=match,
        **base,
    )


def test_first_matching_rule_wins_by_priority() -> None:
    rules = [
        _rule(10, {"archetype": ["One-and-done"]}, message_angle="winback_habit", urgency="high"),
        _rule(20, {}, message_angle="winback_flexible", urgency="low"),
    ]
    # The order the rules are passed in must not matter; priority decides.
    result = resolve({"archetype": "One-and-done"}, list(reversed(rules)))
    assert result.message_angle == "winback_habit"
    assert result.urgency == "high"
    assert (result.rule_id, result.rule_name, result.version) == (10, "rule_10", 1)


def test_wildcard_rule_catches_what_earlier_rules_miss() -> None:
    rules = [
        _rule(10, {"archetype": ["Frequent (5+, censored)"]}, message_angle="winback_habit"),
        _rule(20, {}, message_angle="winback_flexible", prompt_variant="flexible_minimal"),
    ]
    result = resolve({"archetype": "None observed"}, rules)
    assert result.message_angle == "winback_flexible"
    assert result.prompt_variant == "flexible_minimal"


def test_a_boolean_feature_matches_its_string_value() -> None:
    rules = [
        _rule(
            10,
            {"archetype": ["One-and-done"], "history_censored": ["true"]},
            prompt_variant="flexible_soft",
        ),
        _rule(20, {}, prompt_variant="flexible_standard"),
    ]
    censored = resolve({"archetype": "One-and-done", "history_censored": True}, rules)
    clean = resolve({"archetype": "One-and-done", "history_censored": False}, rules)
    assert censored.prompt_variant == "flexible_soft"
    assert clean.prompt_variant == "flexible_standard"


def test_only_allowlisted_features_are_read() -> None:
    rules = [_rule(10, {"archetype": ["One-and-done"]}, message_angle="winback_habit")]
    # client_id and a raw amount are not allow-listed and must not affect matching.
    result = resolve(
        {"archetype": "One-and-done", "client_id": 1001, "total_purchase_amount": 999999},
        rules,
    )
    assert result.message_angle == "winback_habit"


def test_no_matching_rule_raises() -> None:
    rules = [_rule(10, {"archetype": ["Frequent (5+, censored)"]})]
    with pytest.raises(NoRuleMatched):
        resolve({"archetype": "One-and-done"}, rules)


def test_feature_view_keeps_only_allowlisted_bucket_features() -> None:
    row = ClientFeatures(
        client_id=1001,
        archetype="One-and-done",
        recency_bucket="Exited 3y plus",
        value_tier="High",
        rhythm_band="Unknown",
        own_rhythm_days=42,
        observed_volume=1,
        purchases_censored=False,
        history_censored=True,
    )
    view = feature_view(row)
    assert view == {
        "archetype": "One-and-done",
        "recency_bucket": "Exited 3y plus",
        "value_tier": "High",
        "rhythm_band": "Unknown",
        "purchases_censored": "false",
        "history_censored": "true",
    }


@pytest.mark.parametrize(
    ("features", "expected_rule", "angle", "tier"),
    [
        (
            {"archetype": "Frequent (5+, censored)", "value_tier": "Top"},
            "frequent_high_value",
            "winback_habit",
            "P1",
        ),
        (
            {"archetype": "Frequent (5+, censored)", "value_tier": "Low"},
            "frequent_default",
            "winback_habit",
            "P2",
        ),
        (
            {"archetype": "One-and-done", "value_tier": "Low"},
            "one_and_done_default",
            "winback_flexible",
            "P3",
        ),
        (
            {"archetype": "None observed", "value_tier": "Mid"},
            "none_observed",
            "winback_flexible",
            "P3",
        ),
    ],
)
def test_seeded_v1_rules_resolve_representative_clients(
    db: None, features: dict[str, Any], expected_rule: str, angle: str, tier: str
) -> None:
    with SessionLocal() as session:
        rules = load_active_rules(session, at=date(2026, 7, 25))
    if not rules:
        pytest.skip("v1 rules not seeded; run alembic upgrade head")
    result: Resolution = resolve(features, rules)
    assert result.rule_name == expected_rule
    assert result.message_angle == angle
    assert result.priority_tier == tier
