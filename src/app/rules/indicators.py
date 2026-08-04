"""Resolve every client to its outreach angle and store the outcome.

Reads the allow-listed features, resolves them against the rules active on a
given date, and upserts one client_message_indicators row per client, keyed by
client_id so a re-run overwrites in place. The winning rule id and version ride
on each row for traceability.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.models import ClientFeatures
from app.db.models.rules import ClientMessageIndicators
from app.rules.engine import Resolution, feature_view, resolve
from app.rules.store import load_active_rules
from app.transform.features import PRIORITY_TIERS

# Columns refreshed when a client's row already exists. The key is excluded.
_INDICATOR_UPDATE = [
    "message_angle",
    "urgency",
    "priority_tier",
    "prompt_variant",
    "rule_id",
    "rule_name",
    "rule_version",
]

# A rule resolving to T1-T4 defers tier and urgency to the feature row rather
# than naming a real value; older rule sets still emit a real P1-P3 and are
# left untouched.
_TIER_URGENCY = {"T1": "high", "T2": "medium", "T3": "medium", "T4": "low"}


def _indicator_dict(feature: ClientFeatures, resolution: Resolution) -> dict[str, Any]:
    priority_tier = resolution.priority_tier
    urgency = resolution.urgency
    if priority_tier in PRIORITY_TIERS:
        priority_tier = feature.priority_tier
        urgency = _TIER_URGENCY[priority_tier]
    return {
        "client_id": feature.client_id,
        "message_angle": resolution.message_angle,
        "urgency": urgency,
        "priority_tier": priority_tier,
        "prompt_variant": resolution.prompt_variant,
        "rule_id": resolution.rule_id,
        "rule_name": resolution.rule_name,
        "rule_version": resolution.version,
    }


def populate_indicators(session: Session, at: date) -> int:
    """Resolve all clients against the rules active on `at` and upsert their rows.

    Returns the number of clients resolved. Raises if no rule set is active,
    since a client with no resolution would be left without an angle.
    """
    rules = load_active_rules(session, at)
    if not rules:
        raise ValueError(f"no active rule version for {at}")

    features = session.scalars(select(ClientFeatures)).all()
    rows = [_indicator_dict(f, resolve(feature_view(f), rules)) for f in features]
    if not rows:
        return 0

    stmt = pg_insert(ClientMessageIndicators).values(rows)
    set_ = {col: getattr(stmt.excluded, col) for col in _INDICATOR_UPDATE}
    set_["resolved_at"] = func.now()
    stmt = stmt.on_conflict_do_update(index_elements=["client_id"], set_=set_)
    session.execute(stmt)
    session.commit()
    return len(rows)
