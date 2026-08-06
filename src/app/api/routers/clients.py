"""Client segment console: browse buckets and see the distribution across them.

Never returns a name or any other PII; that stays behind pii_vault and the
restricted role. Re-attaching a name for an authorized reviewer is part of
the design (§9A.3), but no session or role exists yet (M8.5 is still open),
so this never re-attaches one until that lands.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.clients import ClientSummaryOut, SegmentBucketOut, SegmentDistributionOut
from app.services.clients import ClientNotFound, get_client, list_clients, segment_distribution

router = APIRouter(tags=["clients"])


def _to_summary(row) -> ClientSummaryOut:
    return ClientSummaryOut(
        client_id=row.client_id,
        unit_fund_id=row.unit_fund_id,
        recency_band=row.recency_band,
        value_band=row.value_band,
        cadence_band=row.cadence_band,
        hold_band=row.hold_band,
        message_angle=row.message_angle,
        priority_tier=row.priority_tier,
    )


@router.get("/clients", response_model=Page[ClientSummaryOut])
def get_clients(
    client_id: int | None = None,
    fund_id: int | None = None,
    value_band: str | None = None,
    recency_band: str | None = None,
    purchase_depth: str | None = None,
    message_angle: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[ClientSummaryOut]:
    """Clients matching the given bucket filters. Buckets only, never a name."""
    try:
        rows, next_cursor = list_clients(
            session,
            client_id=client_id,
            fund_id=fund_id,
            value_band=value_band,
            recency_band=recency_band,
            purchase_depth=purchase_depth,
            message_angle=message_angle,
            cursor=cursor,
            limit=limit,
        )
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(items=[_to_summary(r) for r in rows], next_cursor=next_cursor)


@router.get("/clients/{client_id}", response_model=ClientSummaryOut)
def get_client_detail(client_id: int, session: Session = Depends(get_session)) -> ClientSummaryOut:
    """One client's buckets. Buckets only, never a name (see module docstring)."""
    try:
        row = get_client(session, client_id)
    except ClientNotFound:
        raise HTTPException(status_code=404, detail="client not found") from None
    return _to_summary(row)


@router.get("/segments", response_model=SegmentDistributionOut)
def get_segments(session: Session = Depends(get_session)) -> SegmentDistributionOut:
    """Client counts grouped by purchase depth, value band, and message angle."""
    distribution = segment_distribution(session)
    return SegmentDistributionOut(
        by_purchase_depth=[
            SegmentBucketOut(key=k, count=c) for k, c in distribution["by_purchase_depth"]
        ],
        by_value_band=[SegmentBucketOut(key=k, count=c) for k, c in distribution["by_value_band"]],
        by_message_angle=[
            SegmentBucketOut(key=k, count=c) for k, c in distribution["by_message_angle"]
        ],
        stale_contact_count=distribution["stale_contact_count"],
    )
