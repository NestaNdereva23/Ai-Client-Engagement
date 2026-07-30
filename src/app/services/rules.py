"""Console reads over the versioned business-rule store, and a stateless preview.

Both functions are thin wrappers over app.rules: list_rule_versions collapses
business_rules into one summary row per version, and preview reuses the same
load_active_rules + resolve pair the generation pipeline itself calls, so a
dry run always answers with the rule set that would actually be used.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models.rules import BusinessRule
from app.rules.engine import Resolution, resolve
from app.rules.store import load_active_rules


def list_rule_versions(
    session: Session, *, version: int | None = None, active_on: date | None = None
) -> list[tuple[int, date, date | None, int]]:
    """One row per version: (version, valid_from, valid_to, rule_count)."""
    query = select(
        BusinessRule.version,
        func.min(BusinessRule.valid_from),
        func.max(BusinessRule.valid_to),
        func.count(),
    ).group_by(BusinessRule.version)
    if version is not None:
        query = query.where(BusinessRule.version == version)
    if active_on is not None:
        query = query.having(func.min(BusinessRule.valid_from) <= active_on).having(
            or_(
                func.max(BusinessRule.valid_to).is_(None),
                func.max(BusinessRule.valid_to) > active_on,
            )
        )
    query = query.order_by(BusinessRule.version.desc())
    return list(session.execute(query).all())


def preview(session: Session, features: Mapping[str, Any], *, at: date) -> Resolution:
    """Dry-run a feature tuple through the rule set active on `at`. May raise NoRuleMatched."""
    rules = load_active_rules(session, at)
    return resolve(features, rules)
