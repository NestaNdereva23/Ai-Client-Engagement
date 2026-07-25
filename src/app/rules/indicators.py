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


def _indicator_dict(client_id: int, resolution: Resolution) -> dict[str, Any]:
    return {
        "client_id": client_id,
        "message_angle": resolution.message_angle,
        "urgency": resolution.urgency,
        "priority_tier": resolution.priority_tier,
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
    rows = [_indicator_dict(f.client_id, resolve(feature_view(f), rules)) for f in features]
    if not rows:
        return 0

    stmt = pg_insert(ClientMessageIndicators).values(rows)
    set_ = {col: getattr(stmt.excluded, col) for col in _INDICATOR_UPDATE}
    set_["resolved_at"] = func.now()
    stmt = stmt.on_conflict_do_update(index_elements=["client_id"], set_=set_)
    session.execute(stmt)
    session.commit()
    return len(rows)
