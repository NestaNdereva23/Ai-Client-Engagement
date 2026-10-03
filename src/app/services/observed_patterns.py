from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.observed_pattern import ObservedPattern
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_id_cursor, encode_id_cursor


class PatternNotFound(Exception):
    pass


def _filters(*, status: str | None, direction: str | None) -> list[Any]:
    clauses: list[Any] = []
    if status is not None:
        clauses.append(ObservedPattern.status == status)
    if direction is not None:
        clauses.append(ObservedPattern.direction == direction)
    return clauses


def list_patterns(
    session: Session,
    *,
    status: str | None = None,
    direction: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[ObservedPattern], str | None]:
    limit = clamp_limit(limit)
    query = select(ObservedPattern).where(*_filters(status=status, direction=direction))
    if cursor is not None:
        query = query.where(ObservedPattern.pattern_id < decode_id_cursor(cursor))
    rows = list(
        session.scalars(query.order_by(ObservedPattern.pattern_id.desc()).limit(limit + 1)).all()
    )
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_id_cursor(rows[-1].pattern_id)
    return rows, next_cursor


def count_patterns(
    session: Session, *, status: str | None = None, direction: str | None = None
) -> int:
    return session.scalar(
        select(func.count())
        .select_from(ObservedPattern)
        .where(*_filters(status=status, direction=direction))
    )


def count_patterns_by_status(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(ObservedPattern.status, func.count()).group_by(ObservedPattern.status)
    ).all()
    return {status: count for status, count in rows}


def get_pattern(session: Session, pattern_id: int) -> ObservedPattern:
    pattern = session.get(ObservedPattern, pattern_id)
    if pattern is None:
        raise PatternNotFound(pattern_id)
    return pattern


def review_pattern(
    session: Session, pattern_id: int, *, status: str, reviewed_by: str
) -> ObservedPattern:
    pattern = get_pattern(session, pattern_id)
    earlier_status = pattern.status
    pattern.status = status
    pattern.reviewed_by = reviewed_by
    pattern.reviewed_at = datetime.now(UTC)
    record_audit(
        session,
        entity_type="observed_pattern",
        action="review",
        entity_id=str(pattern_id),
        actor_id=reviewed_by,
        detail={"from": earlier_status, "to": status},
    )
    return pattern
