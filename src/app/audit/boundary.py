"""Persist model-boundary crossings to the append-only audit trail."""

from __future__ import annotations

from app.audit.log import record_audit
from app.db.session import SessionLocal
from app.privacy.boundary import BoundaryAudit


def audit_boundary_crossing(record: BoundaryAudit) -> None:
    """Append one audit row for a model boundary crossing.

    Runs in its own transaction so the row persists even when the crossing was
    blocked and the caller aborts.
    """
    with SessionLocal() as session:
        record_audit(
            session,
            entity_type="model_boundary",
            action="model_call",
            entity_id=record.entity_id,
            run_id=record.run_id,
            trace_id=record.trace_id,
            detail={
                "fields": record.fields,
                "inbound": record.inbound,
                "outbound": record.outbound,
                "reason": record.reason,
            },
        )
        session.commit()
