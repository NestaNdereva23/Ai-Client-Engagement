from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.clients import (
    ClientActivityOut,
    ClientBandsOut,
    ClientBookSummaryOut,
    ClientContactEventOut,
    ClientEnrollmentOut,
    ClientFlagsOut,
    ClientIdentityOut,
    ClientNameOut,
    ClientOutreachMessageOut,
    ClientProfileOut,
    ClientRoutingOut,
    ClientSummaryOut,
    ClientSuppressionOut,
    ClientTouchOut,
    EnrollmentSummaryOut,
    SegmentBucketOut,
    SegmentDistributionOut,
    SuppressionReasonCountOut,
    SuppressionSummaryOut,
    ValueRecencyBucketOut,
)
from app.services.clients import (
    ClientNotFound,
    ClientProfile,
    client_book_summary,
    enrollment_summary,
    get_client,
    get_client_name,
    get_client_profile,
    latest_call_brief,
    list_clients,
    segment_distribution,
    suppression_summary,
)

router = APIRouter(tags=["clients"], dependencies=[Depends(get_current_reviewer_id)])


def _to_summary(row, *, call_brief: str | None = None) -> ClientSummaryOut:
    return ClientSummaryOut(
        client_id=row.client_id,
        unit_fund_id=row.unit_fund_id,
        recency_band=row.recency_band,
        value_band=row.value_band,
        cadence_band=row.cadence_band,
        hold_band=row.hold_band,
        purchase_depth=row.purchase_depth,
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
    cadence_band: str | None = None,
    message_angle: str | None = None,
    newly_dormant: bool | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[ClientSummaryOut]:
    try:
        rows, next_cursor = list_clients(
            session,
            client_id=client_id,
            fund_id=fund_id,
            value_band=value_band,
            recency_band=recency_band,
            purchase_depth=purchase_depth,
            cadence_band=cadence_band,
            message_angle=message_angle,
            newly_dormant=newly_dormant,
            cursor=cursor,
            limit=limit,
        )
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(items=[_to_summary(r) for r in rows], next_cursor=next_cursor)


@router.get("/clients/summary", response_model=ClientBookSummaryOut)
def get_clients_summary(session: Session = Depends(get_session)) -> ClientBookSummaryOut:
    summary = client_book_summary(session)
    return ClientBookSummaryOut(total_clients=summary.total_clients, fund_count=summary.fund_count)


@router.get("/clients/enrollment-summary", response_model=EnrollmentSummaryOut)
def get_clients_enrollment_summary(
    session: Session = Depends(get_session),
) -> EnrollmentSummaryOut:
    summary = enrollment_summary(session)
    return EnrollmentSummaryOut(
        enrolled_count=summary.enrolled_count, excluded_count=summary.excluded_count
    )


@router.get("/clients/suppression-summary", response_model=SuppressionSummaryOut)
def get_clients_suppression_summary(
    session: Session = Depends(get_session),
) -> SuppressionSummaryOut:
    summary = suppression_summary(session)
    return SuppressionSummaryOut(
        suppressed_count=summary.suppressed_count,
        by_reason=[
            SuppressionReasonCountOut(reason=reason, count=count)
            for reason, count in summary.by_reason
        ],
    )


@router.get("/clients/{client_id}", response_model=ClientSummaryOut)
def get_client_detail(client_id: int, session: Session = Depends(get_session)) -> ClientSummaryOut:
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
    try:
        profile = get_client_profile(session, client_id)
    except ClientNotFound:
        raise HTTPException(status_code=404, detail="client not found") from None
    return _to_profile_out(profile)


@router.get("/clients/{client_id}/name", response_model=ClientNameOut)
def get_client_name_detail(
    client_id: int,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> ClientNameOut:
    try:
        name = get_client_name(session, client_id, reviewer_id=reviewer_id)
    except ClientNotFound:
        raise HTTPException(status_code=404, detail="client not found") from None
    return ClientNameOut(client_id=client_id, client_name=name)


@router.get("/segments", response_model=SegmentDistributionOut)
def get_segments(session: Session = Depends(get_session)) -> SegmentDistributionOut:
    distribution = segment_distribution(session)
    return SegmentDistributionOut(
        by_purchase_depth=[
            SegmentBucketOut(key=k, count=c) for k, c in distribution["by_purchase_depth"]
        ],
        by_value_band=[SegmentBucketOut(key=k, count=c) for k, c in distribution["by_value_band"]],
        by_cadence_band=[
            SegmentBucketOut(key=k, count=c) for k, c in distribution["by_cadence_band"]
        ],
        by_message_angle=[
            SegmentBucketOut(key=k, count=c) for k, c in distribution["by_message_angle"]
        ],
        by_value_and_recency=[
            ValueRecencyBucketOut(value_band=v, recency_band=r, count=c)
            for v, r, c in distribution["by_value_and_recency"]
        ],
        stale_contact_count=distribution["stale_contact_count"],
        history_censored_count=distribution["history_censored_count"],
        purchases_censored_count=distribution["purchases_censored_count"],
        unknown_recency_count=distribution["unknown_recency_count"],
    )
