"""The shared writer for the append-only audit trail.

Every audited action goes through record_audit. It only ever inserts, matching
the INSERT/SELECT-only grant on audit_log.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.db.models.audit import AuditLog


def record_audit(
    session: Session,
    *,
    entity_type: str,
    action: str,
    entity_id: str | None = None,
    actor_id: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> AuditLog:
    """Insert one audit row and return it. The caller owns the transaction."""
    row = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_id=actor_id,
        run_id=run_id,
        trace_id=trace_id,
        detail=dict(detail) if detail is not None else None,
    )
    session.add(row)
    session.flush()
    return row
