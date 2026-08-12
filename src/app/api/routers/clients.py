"""Client segment console: browse buckets and see the distribution across them.

Never returns a name or any other PII; that stays behind pii_vault and the
restricted role. Re-attaching a name for an authorized reviewer is part of
the design (§9A.3), but no session or role exists yet (M8.5 is still open),
so this never re-attaches one until that lands.

The one exception is call_brief, on the single-client detail read and the
fuller profile read: it carries no name and no PII to begin with, so this is
not name re-attachment.

GET /clients/{id}/profile is the fuller read: identity, behavioural bands,
flags, activity, routing, and every campaign/engagement record this codebase
holds for one client. Still no name, and still nothing an outreach_message's
own drafted or personalized content would carry -- only its status history.
It is a separate, explicitly named endpoint rather than a widened
GET /clients/{id}, so an existing caller's response shape never changes
under it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.clients import (
    ClientActivityOut,
    ClientBandsOut,
    ClientContactEventOut,
    ClientEnrollmentOut,
    ClientFlagsOut,
    ClientIdentityOut,
    ClientOutreachMessageOut,
    ClientProfileOut,
    ClientRoutingOut,
    ClientSummaryOut,
    ClientSuppressionOut,
    ClientTouchOut,
    SegmentBucketOut,
    SegmentDistributionOut,
)
from app.services.clients import (
    ClientNotFound,
    ClientProfile,
    get_client,
    get_client_profile,
    latest_call_brief,
    list_clients,
    segment_distribution,
)

router = APIRouter(tags=["clients"])


def _to_summary(row, *, call_brief: str | None = None) -> ClientSummaryOut:
    return ClientSummaryOut(
        client_id=row.client_id,
        unit_fund_id=row.unit_fund_id,
        recency_band=row.recency_band,
        value_band=row.value_band,
        cadence_band=row.cadence_band,
        hold_band=row.hold_band,
        message_angle=row.message_angle,
        priority_tier=row.priority_tier,
        call_brief=call_brief,
    )


@router.get("/clients", response_model=Page[ClientSummaryOut])
def get_clients(
    client_id: int | None = None,
    fund_id: int | None = None,
    value_band: str | None = None,
    recency_band: str | None = None,
    purchase_depth: str | None = None,
    message_angle: str | None = None,
    newly_dormant: bool | None = None,
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
            newly_dormant=newly_dormant,
            cursor=cursor,
            limit=limit,
        )
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(items=[_to_summary(r) for r in rows], next_cursor=next_cursor)


@router.get("/clients/{client_id}", response_model=ClientSummaryOut)
def get_client_detail(client_id: int, session: Session = Depends(get_session)) -> ClientSummaryOut:
    """One client's buckets, plus their latest approved call_brief if one
    exists (see module docstring for why that is not PII re-attachment)."""
    try:
        row = get_client(session, client_id)
    except ClientNotFound:
        raise HTTPException(status_code=404, detail="client not found") from None
    return _to_summary(row, call_brief=latest_call_brief(session, client_id))


def _to_profile_out(profile: ClientProfile) -> ClientProfileOut:
    core = profile.core
    suppression = profile.suppression
    return ClientProfileOut(
        identity=ClientIdentityOut(
            client_id=core.client_id,
            client_code=core.client_code,
            unit_fund_id=core.unit_fund_id,
            fund_name=core.unit_fund_name,
            fund_type=core.fund_type,
            n_funds=core.n_funds,
            holds_other_funds=core.holds_other_funds,
        ),
        bands=ClientBandsOut(
            recency_band=core.recency_band,
            value_band=core.value_band,
            cadence_band=core.cadence_band,
            hold_band=core.hold_band,
            purchase_depth=core.purchase_depth,
            trend_band=core.trend_band,
            exit_reason=core.exit_reason,
            own_rhythm_days=core.own_rhythm_days,
        ),
        flags=ClientFlagsOut(
            in_wave=core.in_wave,
            newly_dormant=core.newly_dormant,
            has_depth=core.has_depth,
            staged_exit=core.staged_exit,
            stale_contact=core.stale_contact,
            history_censored=core.history_censored,
            purchases_censored=core.purchases_censored,
        ),
        activity=ClientActivityOut(
            last_activity_date=core.last_activity_date,
            days_since_last_activity=core.days_since_last_activity,
            observed_volume=core.observed_volume,
            n_purchases_returned=core.n_purchases_returned,
            total_purchase_amount=core.total_purchase_amount,
            computed_at=core.computed_at,
        ),
        routing=ClientRoutingOut(
            message_angle=core.message_angle,
            priority_tier=core.priority_tier,
            urgency=core.urgency,
            prompt_variant=core.prompt_variant,
            rule_name=core.rule_name,
            rule_version=core.rule_version,
        ),
        enrollments=[
            ClientEnrollmentOut(
                enrollment_id=e.enrollment_id,
                campaign_id=e.campaign_id,
                status=e.status,
                current_step=e.current_step,
                next_due_at=e.next_due_at,
                enrolled_at=e.enrolled_at,
                is_primary_contact_row=e.is_primary_contact_row,
            )
            for e in profile.enrollments
        ],
        touch_log=[
            ClientTouchOut(
                touch_id=t.touch_id,
                enrollment_id=t.enrollment_id,
                step_no=t.step_no,
                message_id=t.message_id,
                sent_at=t.sent_at,
                delivery_status=t.delivery_status,
                created_at=t.created_at,
            )
            for t in profile.touch_log
        ],
        outreach_messages=[
            ClientOutreachMessageOut(
                message_id=m.message_id,
                campaign_id=m.campaign_id,
                template_id=m.template_id,
                channel=m.channel,
                status=m.status,
                created_at=m.created_at,
                updated_at=m.updated_at,
            )
            for m in profile.outreach_messages
        ],
        contact_events=[
            ClientContactEventOut(
                id=c.id, type=c.type, occurred_at=c.occurred_at, created_at=c.created_at
            )
            for c in profile.contact_events
        ],
        suppression=ClientSuppressionOut(
            is_suppressed=suppression is not None,
            reason=suppression.reason if suppression else None,
            source=suppression.source if suppression else None,
            created_at=suppression.created_at if suppression else None,
        ),
        call_brief=profile.call_brief,
    )


@router.get("/clients/{client_id}/profile", response_model=ClientProfileOut)
def get_client_profile_detail(
    client_id: int, session: Session = Depends(get_session)
) -> ClientProfileOut:
    """The fuller, non-PII client profile (see module docstring). Safe to
    call with no auth: every field here is confirmed non-PII, unlike a
    client's name, which this endpoint does not carry.
    """
    try:
        profile = get_client_profile(session, client_id)
    except ClientNotFound:
        raise HTTPException(status_code=404, detail="client not found") from None
    return _to_profile_out(profile)


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
