"""Console reads over the append-only audit trail, and trace lookups."""

from __future__ import annotations

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.db.models.audit import AuditLog
from app.db.models.llmops import TraceRef
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_cursor, encode_cursor


class TraceNotFound(Exception):
    """No trace_refs row exists with the given trace id."""


def browse_audit_log(
    session: Session,
    *,
    entity_type: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[AuditLog], str | None]:
    """Audit rows newest first, one page at a time."""
    limit = clamp_limit(limit)
    query = select(AuditLog)
    if entity_type is not None:
        query = query.where(AuditLog.entity_type == entity_type)
    if run_id is not None:
        query = query.where(AuditLog.run_id == run_id)
    if trace_id is not None:
        query = query.where(AuditLog.trace_id == trace_id)
    if cursor is not None:
        before_created_at, before_id = decode_cursor(cursor)
        query = query.where(
            tuple_(AuditLog.created_at, AuditLog.log_id) < (before_created_at, int(before_id))
        )
    query = query.order_by(AuditLog.created_at.desc(), AuditLog.log_id.desc()).limit(limit + 1)
    rows = list(session.scalars(query).all())

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.created_at, str(last.log_id))
    return rows, next_cursor


def get_trace(session: Session, trace_id: str) -> TraceRef:
    """One trace_refs row by its Langfuse trace id, or raise TraceNotFound."""
    trace = session.scalar(select(TraceRef).where(TraceRef.trace_id == trace_id))
    if trace is None:
        raise TraceNotFound(trace_id)
    return trace
