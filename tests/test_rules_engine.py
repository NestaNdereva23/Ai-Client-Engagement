"""First-match-wins resolution over the business rules.

The pure tests build rules in memory to check ordering, the wildcard fallback,
boolean matching, and that only allow-listed features are read.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.db.models.models import ClientFeatures
from app.db.models.rules import BusinessRule
from app.rules.engine import NoRuleMatched, feature_view, resolve


def _rule(priority: int, match: dict[str, Any], **outputs: str) -> BusinessRule:
    base = {
        "message_angle": "pick_up_again",
        "urgency": "low",
        "priority_tier": "T4",
        "prompt_variant": "pick_up_again",
    }
    base.update(outputs)
    return BusinessRule(
        rule_id=priority,
        version=3,
        priority=priority,
        name=f"rule_{priority}",
        match=match,
        **base,
    )


def test_first_matching_rule_wins_by_priority() -> None:
    rules = [
        _rule(10, {"value_band": ["Top"]}, message_angle="back_on_schedule", urgency="high"),
        _rule(20, {}, message_angle="pick_up_again", urgency="low"),
    ]
    # The order the rules are passed in must not matter; priority decides.
    result = resolve({"value_band": "Top"}, list(reversed(rules)))
    assert result.message_angle == "back_on_schedule"
    assert result.urgency == "high"
    assert (result.rule_id, result.rule_name, result.version) == (10, "rule_10", 3)


def test_wildcard_rule_catches_what_earlier_rules_miss() -> None:
    rules = [
        _rule(10, {"value_band": ["Top"]}, message_angle="back_on_schedule"),
        _rule(20, {}, message_angle="pick_up_again", prompt_variant="pick_up_again"),
    ]
    result = resolve({"value_band": "Low"}, rules)
    assert result.message_angle == "pick_up_again"
    assert result.prompt_variant == "pick_up_again"


def test_a_boolean_feature_matches_its_string_value() -> None:
    rules = [
        _rule(
            10,
            {"value_band": ["Top"], "history_censored": ["true"]},
            prompt_variant="second_try",
        ),
        _rule(20, {}, prompt_variant="pick_up_again"),
    ]
    censored = resolve({"value_band": "Top", "history_censored": True}, rules)
    clean = resolve({"value_band": "Top", "history_censored": False}, rules)
    assert censored.prompt_variant == "second_try"
    assert clean.prompt_variant == "pick_up_again"


def test_only_allowlisted_features_are_read() -> None:
    rules = [_rule(10, {"value_band": ["Top"]}, message_angle="back_on_schedule")]
    # client_id and a raw amount are not allow-listed and must not affect matching.
    result = resolve(
        {"value_band": "Top", "client_id": 1001, "total_purchase_amount": 999999},
        rules,
    )
    assert result.message_angle == "back_on_schedule"


def test_no_matching_rule_raises() -> None:
    rules = [_rule(10, {"value_band": ["Top"]})]
    with pytest.raises(NoRuleMatched):
        resolve({"value_band": "Low"}, rules)


def test_feature_view_keeps_only_allowlisted_bucket_features() -> None:
    row = ClientFeatures(
        client_id=1001,
        recency_band="Over 6y",
        value_band="High",
        cadence_band="None",
        hold_band="Unknown",
        purchase_depth="single",
        trend_band="unknown",
        exit_reason="unknown",
        fund_type="other",
        in_wave=False,
        has_depth=False,
        staged_exit=False,
        stale_contact=False,
        holds_other_funds=False,
        own_rhythm_days=42,
        observed_volume=1,
        purchases_censored=False,
        history_censored=True,
    )
    view = feature_view(row)
    assert view == {
        "recency_band": "Over 6y",
        "value_band": "High",
        "cadence_band": "None",
        "hold_band": "Unknown",
        "purchase_depth": "single",
        "trend_band": "unknown",
        "exit_reason": "unknown",
        "fund_type": "other",
        "in_wave": "false",
        "has_depth": "false",
        "staged_exit": "false",
        "stale_contact": "false",
        "purchases_censored": "false",
        "history_censored": "true",
        "holds_other_funds": "false",
    }
