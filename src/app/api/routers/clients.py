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
from app.services.clients import list_clients, segment_distribution

router = APIRouter(tags=["clients"])


@router.get("/clients", response_model=Page[ClientSummaryOut])
def get_clients(
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
    items = [
        ClientSummaryOut(
            client_id=r.client_id,
            unit_fund_id=r.unit_fund_id,
            recency_band=r.recency_band,
            value_band=r.value_band,
            cadence_band=r.cadence_band,
            hold_band=r.hold_band,
            message_angle=r.message_angle,
            priority_tier=r.priority_tier,
        )
        for r in rows
    ]
    return Page(items=items, next_cursor=next_cursor)


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
