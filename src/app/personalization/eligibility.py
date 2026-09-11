from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models.prompt_config import FactEligibilityRule
from app.privacy.fact_block import ModelFactBlock

EXPOSURE_MODES = ("direct", "placeholder", "conditional", "prohibited")

FACT_FIELDS = tuple(ModelFactBlock.model_fields.keys())

FEE_WARNING_ANGLE = "fee_warning"

# The six fields a bucketed template stands in with a token instead of a real
# number, in ModelFactBlock's own names.
PLACEHOLDER_FIELDS = (
    "typical_contribution_kes",
    "largest_contribution_kes",
    "years_since_exit",
    "days_held_after_last_topup",
    "month_they_left",
    "invested_every_n_days",
)

# Only these three spell differently as a prompt token than as a schema field.
_TOKEN_NAMES: dict[str, str] = {
    "typical_contribution_kes": "typical_contribution",
    "largest_contribution_kes": "largest_contribution",
    "invested_every_n_days": "cadence_interval_days",
}


def token_name(fact_field: str) -> str:
    return _TOKEN_NAMES.get(fact_field, fact_field)


PLACEHOLDER_FACT_FIELDS = tuple(token_name(field) for field in PLACEHOLDER_FIELDS)

# A conditional field's own check, by field name; conditional_prohibitions()
# runs these same checks inline today. A field with no entry here is treated
# as met whenever it is present at all.
_CONDITIONAL_FIELD_CHECKS: dict[str, Callable[[Mapping[str, Any]], bool]] = {
    "month_the_account_empties": lambda facts: facts.get("month_the_account_empties") is not None,
}


class FactEligibilityError(ValueError):
    pass


def resolve_fact_eligibility(
    session: Session, angle: str | None, policy_version: int
) -> dict[str, str]:
    rows = session.scalars(
        select(FactEligibilityRule).where(
            FactEligibilityRule.policy_version == policy_version,
            or_(FactEligibilityRule.angle.is_(None), FactEligibilityRule.angle == angle),
        )
    ).all()

    resolved = {fact_field: "prohibited" for fact_field in FACT_FIELDS}
    for row in rows:
        if row.angle is None and row.fact_field in resolved:
            resolved[row.fact_field] = row.exposure_mode
    for row in rows:
        if row.angle is not None and row.fact_field in resolved:
            resolved[row.fact_field] = row.exposure_mode
    return resolved


def save_fact_eligibility_rule(
    session: Session,
    policy_version: int,
    fact_field: str,
    exposure_mode: str,
    *,
    angle: str | None = None,
    condition: str | None = None,
) -> FactEligibilityRule:
    if fact_field not in FACT_FIELDS:
        raise FactEligibilityError(f"'{fact_field}' is not a ModelFactBlock field")
    if exposure_mode not in EXPOSURE_MODES:
        raise FactEligibilityError(f"'{exposure_mode}' is not a recognised exposure mode")

    row = FactEligibilityRule(
        policy_version=policy_version,
        angle=angle,
        fact_field=fact_field,
        exposure_mode=exposure_mode,
        condition=condition,
    )
    session.add(row)
    session.flush()
    return row


@dataclass(frozen=True)
class FilteredFacts:
    direct: dict[str, Any] = field(default_factory=dict)
    placeholder: dict[str, Any] = field(default_factory=dict)


def filter_facts_for_prompt(
    facts: Mapping[str, Any], eligibility: Mapping[str, str]
) -> FilteredFacts:
    direct: dict[str, Any] = {}
    placeholder: dict[str, Any] = {}
    for fact_field, value in facts.items():
        mode = eligibility.get(fact_field, "prohibited")
        if mode == "direct":
            direct[fact_field] = value
        elif mode == "placeholder":
            placeholder[fact_field] = token_name(fact_field)
        elif mode == "conditional":
            check = _CONDITIONAL_FIELD_CHECKS.get(fact_field)
            if check is None or check(facts):
                direct[fact_field] = value
    return FilteredFacts(direct=direct, placeholder=placeholder)
