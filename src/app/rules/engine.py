"""Resolve a client's features to one message angle and prompt variant.

A pure, first-match-wins evaluation over an ordered rule set. It reads only the
allow-listed bucket features, never a name, code, or raw figure, so it stays on
the safe side of the model boundary. The winning rule is returned so the caller
can log it for traceability.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.db.models.rules import BusinessRule
from app.rules.store import RULE_FIELD_DOMAINS

# The engine may look at these features and no others.
ALLOWED_FEATURES = frozenset(RULE_FIELD_DOMAINS)


class NoRuleMatched(Exception):
    """No rule matched the features; the set is not exhaustive."""


@dataclass(frozen=True)
class Resolution:
    """The outputs a client resolved to, with the winning rule for the record."""

    message_angle: str
    urgency: str
    priority_tier: str
    prompt_variant: str
    rule_id: int | None
    rule_name: str
    version: int | None


def _as_str(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def feature_view(row: Any) -> dict[str, str]:
    """Take only the allow-listed bucket features from a client_features row."""
    view: dict[str, str] = {}
    for key in ALLOWED_FEATURES:
        value = getattr(row, key, None)
        if value is not None:
            view[key] = _as_str(value)
    return view


def _matches(match: Mapping[str, list[str]], features: Mapping[str, str]) -> bool:
    return all(features.get(key) in set(allowed) for key, allowed in match.items())


def resolve(features: Mapping[str, Any], rules: Sequence[BusinessRule]) -> Resolution:
    """Return the first matching rule's outputs, evaluated in priority order.

    Only allow-listed features are consulted; any other key in `features` is
    ignored, so a stray raw value cannot influence the outcome.
    """
    view = {key: _as_str(value) for key, value in features.items() if key in ALLOWED_FEATURES}
    for rule in sorted(rules, key=lambda r: r.priority):
        if _matches(rule.match, view):
            return Resolution(
                message_angle=rule.message_angle,
                urgency=rule.urgency,
                priority_tier=rule.priority_tier,
                prompt_variant=rule.prompt_variant,
                rule_id=rule.rule_id,
                rule_name=rule.name,
                version=rule.version,
            )
    raise NoRuleMatched(f"no rule matched features {sorted(view)}")
