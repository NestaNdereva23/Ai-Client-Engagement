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
    ClientFeatures.recency_band,
    ClientFeatures.value_band,
    ClientFeatures.cadence_band,
    ClientFeatures.hold_band,
    ClientMessageIndicators.message_angle,
    ClientMessageIndicators.priority_tier,
)


def _base_query(*columns):
    return (
        select(*(columns or _CLIENT_COLUMNS))
        .select_from(Clients)
        .join(ClientFeatures, ClientFeatures.client_id == Clients.client_id, isouter=True)
        .join(
            ClientMessageIndicators,
            ClientMessageIndicators.client_id == Clients.client_id,
            isouter=True,
        )
    )


def _apply_bucket_filters(
    query,
    *,
    client_id: int | None,
    fund_id: int | None,
    value_band: str | None,
    recency_band: str | None,
    purchase_depth: str | None,
    message_angle: str | None,
):
    """The allow-listed bucket filters every client query accepts, applied in
    one place so list_clients, get_client, and cohort resolution for a new
    campaign can never drift apart on what "matching" means.
    """
    if client_id is not None:
        query = query.where(Clients.client_id == client_id)
    if fund_id is not None:
        query = query.where(Clients.unit_fund_id == fund_id)
    if value_band is not None:
        query = query.where(ClientFeatures.value_band == value_band)
    if recency_band is not None:
        query = query.where(ClientFeatures.recency_band == recency_band)
    if purchase_depth is not None:
        query = query.where(ClientFeatures.purchase_depth == purchase_depth)
    if message_angle is not None:
        query = query.where(ClientMessageIndicators.message_angle == message_angle)
    return query


class ClientNotFound(Exception):
    """No client exists with the given id."""


def list_clients(
    session: Session,
    *,
    client_id: int | None = None,
    fund_id: int | None = None,
    value_band: str | None = None,
    recency_band: str | None = None,
    purchase_depth: str | None = None,
    message_angle: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[Row], str | None]:
    """Clients matching the given bucket filters, oldest client_id first."""
    limit = clamp_limit(limit)
    query = _apply_bucket_filters(
        _base_query(),
        client_id=client_id,
        fund_id=fund_id,
        value_band=value_band,
        recency_band=recency_band,
        purchase_depth=purchase_depth,
        message_angle=message_angle,
    )
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


def resolve_cohort_client_ids(
    session: Session,
    *,
    fund_id: int | None = None,
    value_band: str | None = None,
    recency_band: str | None = None,
    purchase_depth: str | None = None,
    message_angle: str | None = None,
) -> list[int]:
    """Every client_id matching the given bucket filters, unpaginated.

    A new campaign's cohort is the whole match, not one page of it, so this
    is the one place a client query intentionally skips the cursor/limit
    every console-facing list uses.
    """
    query = _apply_bucket_filters(
        _base_query(Clients.client_id),
        client_id=None,
        fund_id=fund_id,
        value_band=value_band,
        recency_band=recency_band,
        purchase_depth=purchase_depth,
        message_angle=message_angle,
    )
    return list(session.scalars(query).all())


def get_client(session: Session, client_id: int) -> Row:
    """One client's buckets, or raise ClientNotFound.

    Same non-PII projection as list_clients: a name still isn't re-attached
    here, for the same reason the list endpoint never re-attaches one (see
    module docstring).
    """
    row = session.execute(_base_query().where(Clients.client_id == client_id)).one_or_none()
    if row is None:
        raise ClientNotFound(client_id)
    return row


def segment_distribution(session: Session) -> dict[str, list[tuple[str, int]] | int]:
    """Client counts grouped by purchase depth, value band, and message angle."""

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
        "by_purchase_depth": _counts(ClientFeatures.purchase_depth),
        "by_value_band": _counts(ClientFeatures.value_band),
        "by_message_angle": _counts(ClientMessageIndicators.message_angle),
        "stale_contact_count": stale_count,
    }
