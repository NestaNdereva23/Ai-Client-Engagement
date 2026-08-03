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
from app.transform.features import (
    CADENCE_BANDS,
    EXIT_REASONS,
    FUND_TYPES,
    HOLD_BANDS,
    PURCHASE_DEPTHS,
    RECENCY_BANDS,
    TREND_BANDS,
    VALUE_BANDS,
)

# Allow-listed match fields and the values each may take. The band vocabularies
# come from the derivation itself, so a rule can only name a value that is
# actually produced. Boolean flags accept the strings "true" and "false".
_BOOL = {"true", "false"}
RULE_FIELD_DOMAINS: dict[str, set[str]] = {
    # The original buckets. They keep their meaning while rule sets using them
    # are still in force, and retire only once nothing resolves against them.
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
    # The behavioural bands. Their cut points sit where the angle rules need
    # them, which is what lets an ordered set of equality matches express the
    # whole router without the engine needing to compare numbers.
    "recency_band": set(RECENCY_BANDS),
    "value_band": set(VALUE_BANDS),
    "cadence_band": set(CADENCE_BANDS),
    "hold_band": set(HOLD_BANDS),
    "purchase_depth": set(PURCHASE_DEPTHS),
    "trend_band": set(TREND_BANDS),
    "exit_reason": set(EXIT_REASONS),
    "fund_type": set(FUND_TYPES),
    "in_wave": _BOOL,
    "has_depth": _BOOL,
    "staged_exit": _BOOL,
    "stale_contact": _BOOL,
}

# The twelve angles, plus the two the earlier rule sets resolve to. Kept in step
# with message_angle_catalog, which holds the brief behind each one.
MESSAGE_ANGLES = {
    "winback_habit",
    "winback_flexible",
    "not_a_goodbye",
    "wrong_shelf",
    "see_what_changed",
    "the_long_hold",
    "your_next_deposit",
    "second_try",
    "you_wound_down",
    "you_were_scaling",
    "you_were_fading",
    "back_on_schedule",
    "onboarding_retry",
    "pick_up_again",
}
URGENCIES = {"low", "medium", "high"}
# T1 to T4 are derived from value and recency rather than set by a rule; the
# older P tiers stay while the rule sets that name them are in force.
PRIORITY_TIERS = {"P1", "P2", "P3", "T1", "T2", "T3", "T4"}


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
