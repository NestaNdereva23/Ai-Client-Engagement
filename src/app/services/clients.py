"""Console reads over the client segment: bucketed features only, never PII.

Every query here joins client_features and client_message_indicators, the
model-safe projection the rest of the codebase already treats as the source
of truth for a client's bucket; no query here ever touches pii_vault. A
client's name is deliberately never re-attached: that step is gated on an
authorized role in the design, and no role or session exists yet (M8.5 is
still open), so the safe default until then is to never re-attach one.
"""

from __future__ import annotations

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from app.db.models.models import ClientFeatures, Clients
from app.db.models.rules import ClientMessageIndicators
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_id_cursor, encode_id_cursor

_CLIENT_COLUMNS = (
    Clients.client_id,
    Clients.unit_fund_id,
    ClientFeatures.archetype,
    ClientFeatures.recency_bucket,
    ClientFeatures.value_tier,
    ClientFeatures.rhythm_band,
    ClientMessageIndicators.message_angle,
    ClientMessageIndicators.priority_tier,
)


def _base_query():
    return (
        select(*_CLIENT_COLUMNS)
        .select_from(Clients)
        .join(ClientFeatures, ClientFeatures.client_id == Clients.client_id, isouter=True)
        .join(
            ClientMessageIndicators,
            ClientMessageIndicators.client_id == Clients.client_id,
            isouter=True,
        )
    )


def list_clients(
    session: Session,
    *,
    fund_id: int | None = None,
    archetype: str | None = None,
    value_tier: str | None = None,
    recency_bucket: str | None = None,
    message_angle: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[Row], str | None]:
    """Clients matching the given bucket filters, oldest client_id first."""
    limit = clamp_limit(limit)
    query = _base_query()
    if fund_id is not None:
        query = query.where(Clients.unit_fund_id == fund_id)
    if archetype is not None:
        query = query.where(ClientFeatures.archetype == archetype)
    if value_tier is not None:
        query = query.where(ClientFeatures.value_tier == value_tier)
    if recency_bucket is not None:
        query = query.where(ClientFeatures.recency_bucket == recency_bucket)
    if message_angle is not None:
        query = query.where(ClientMessageIndicators.message_angle == message_angle)
    if cursor is not None:
        after_id = decode_id_cursor(cursor)
        query = query.where(Clients.client_id > after_id)
    query = query.order_by(Clients.client_id).limit(limit + 1)

    rows = list(session.execute(query).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_id_cursor(rows[-1].client_id)
    return rows, next_cursor


def segment_distribution(session: Session) -> dict[str, list[tuple[str, int]] | int]:
    """Client counts grouped by archetype, value tier, and message angle."""

    def _counts(column) -> list[tuple[str, int]]:
        return list(
            session.execute(
                select(column, func.count()).group_by(column).order_by(func.count().desc())
            ).all()
        )

    stale_count = (
        session.execute(
            select(func.count()).select_from(ClientFeatures).where(ClientFeatures.stale_contact)
        ).scalar_one()
        or 0
    )

    return {
        "by_archetype": _counts(ClientFeatures.archetype),
        "by_value_tier": _counts(ClientFeatures.value_tier),
        "by_message_angle": _counts(ClientMessageIndicators.message_angle),
        "stale_contact_count": stale_count,
    }
