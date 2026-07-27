"""Read and write the versioned business-rule store, with validation.

A rule set ships as a numbered version with a validity window and is never
mutated afterwards. Editing means saving a new version. Every write is validated:
match fields and values are known, outputs are in range, and no rule is
unreachable behind an earlier one that already covers it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models.rules import BusinessRule

# Allow-listed match fields and the values each may take. Kept in step with the
# feature buckets; boolean flags accept the strings "true" and "false".
_BOOL = {"true", "false"}
RULE_FIELD_DOMAINS: dict[str, set[str]] = {
    "archetype": {
        "None observed",
        "One-and-done",
        "Occasional (2-4)",
        "Frequent (5+, censored)",
    },
    "recency_bucket": {
        "Unknown",
        "Exited under 1y",
        "Exited 1 to 2y",
        "Exited 2 to 3y",
        "Exited 3y plus",
    },
    "value_tier": {"Top", "High", "Mid", "Low"},
    "rhythm_band": {"Unknown", "Regular", "Periodic", "Infrequent"},
    "history_censored": _BOOL,
    "purchases_censored": _BOOL,
    "holds_other_funds": _BOOL,
}

MESSAGE_ANGLES = {"winback_habit", "winback_flexible"}
URGENCIES = {"low", "medium", "high"}
PRIORITY_TIERS = {"P1", "P2", "P3"}


class RuleValidationError(ValueError):
    """A rule set failed validation and was not written."""


@dataclass(frozen=True)
class RuleSpec:
    """One rule: a match over features and the outputs it resolves to."""

    name: str
    priority: int
    match: Mapping[str, list[str]] = field(default_factory=dict)
    message_angle: str = "winback_flexible"
    urgency: str = "low"
    priority_tier: str = "P3"
    prompt_variant: str = "flexible_standard"


def _covers(broad: Mapping[str, list[str]], narrow: Mapping[str, list[str]]) -> bool:
    """True when every client matching narrow also matches broad.

    broad must constrain no field that narrow leaves open, and on each shared
    field narrow's values must fall inside broad's.
    """
    return all(key in narrow and set(narrow[key]) <= set(values) for key, values in broad.items())


def validate_rules(rules: Sequence[RuleSpec]) -> None:
    """Raise RuleValidationError unless the rule set is well formed and reachable."""
    if not rules:
        raise RuleValidationError("a rule set may not be empty")

    priorities = [r.priority for r in rules]
    if len(set(priorities)) != len(priorities):
        raise RuleValidationError("rule priorities must be unique within a version")

    for rule in rules:
        for name, values in rule.match.items():
            if name not in RULE_FIELD_DOMAINS:
                raise RuleValidationError(f"rule '{rule.name}' matches unknown field '{name}'")
            if not values:
                raise RuleValidationError(
                    f"rule '{rule.name}' has an empty value list for '{name}'"
                )
            outside = sorted(set(values) - RULE_FIELD_DOMAINS[name])
            if outside:
                raise RuleValidationError(
                    f"rule '{rule.name}' field '{name}' has values outside its range: {outside}"
                )
        if rule.message_angle not in MESSAGE_ANGLES:
            raise RuleValidationError(
                f"rule '{rule.name}' has unknown angle '{rule.message_angle}'"
            )
        if rule.urgency not in URGENCIES:
            raise RuleValidationError(f"rule '{rule.name}' has unknown urgency '{rule.urgency}'")
        if rule.priority_tier not in PRIORITY_TIERS:
            raise RuleValidationError(
                f"rule '{rule.name}' has unknown priority tier '{rule.priority_tier}'"
            )
        if not rule.prompt_variant:
            raise RuleValidationError(f"rule '{rule.name}' has no prompt variant")

    ordered = sorted(rules, key=lambda r: r.priority)
    for i, later in enumerate(ordered):
        for earlier in ordered[:i]:
            if _covers(earlier.match, later.match):
                raise RuleValidationError(
                    f"rule '{later.name}' is unreachable; '{earlier.name}' already covers it"
                )


def save_version(
    session: Session,
    version: int,
    rules: Sequence[RuleSpec],
    *,
    valid_from: date,
    valid_to: date | None = None,
) -> int:
    """Validate and insert a new rule-set version, returning the row count.

    Refuses to touch a version that already exists, so a shipped set is never
    mutated. A later valid_from simply supersedes the one before it.
    """
    validate_rules(rules)

    if session.scalar(select(func.count()).where(BusinessRule.version == version)):
        raise RuleValidationError(f"version {version} already exists and may not be mutated")

    rows = [
        BusinessRule(
            version=version,
            priority=rule.priority,
            name=rule.name,
            match=dict(rule.match),
            message_angle=rule.message_angle,
            urgency=rule.urgency,
            priority_tier=rule.priority_tier,
            prompt_variant=rule.prompt_variant,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        for rule in rules
    ]
    session.add_all(rows)
    session.flush()
    return len(rows)


def load_active_rules(session: Session, at: date) -> list[BusinessRule]:
    """Return the active version's rules for `at`, ordered by priority.

    The active version is the one with the latest valid_from that has started
    and not ended by `at`; a higher version breaks a tie.
    """
    version = session.scalar(
        select(BusinessRule.version)
        .where(
            BusinessRule.valid_from <= at,
            or_(BusinessRule.valid_to.is_(None), BusinessRule.valid_to > at),
        )
        .order_by(BusinessRule.valid_from.desc(), BusinessRule.version.desc())
        .limit(1)
    )
    if version is None:
        return []
    return list(
        session.scalars(
            select(BusinessRule)
            .where(BusinessRule.version == version)
            .order_by(BusinessRule.priority)
        ).all()
    )
