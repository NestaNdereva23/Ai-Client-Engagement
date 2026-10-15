from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.access import Access, book_fa_id, get_access, require_client_in_book
from app.db.models.routing import ClientTier
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.risk.signals import advisor_signal_tags, fired_signal_tags
from app.routing.tier_playbook import SEGMENT_PLAYBOOK, TIER_PLAYBOOK, tier_tag_fields
from app.routing.tiers import (
    ACTIVE_TIERS_IN_DISPLAY_ORDER,
    BELOW_CAPITAL_PRESERVATION_THRESHOLD,
    NO_TRANSACTIONS_IN_REVIEW_PERIOD,
    SEGMENT_BY_TIER,
)
from app.schemas.active_clients import (
    ActiveClientBandsOut,
    ActiveClientComplaintOut,
    ActiveClientIdentityOut,
    ActiveClientProfileOut,
    ActiveClientRiskHistoryEntryOut,
    ActiveClientRosterLineOut,
    ActiveTransactionOut,
    AiActionOut,
    DepositPercentileOut,
    InteractionCreate,
    InteractionOut,
    ReviewPeriodActivityOut,
    RouteChangeDetailOut,
    RouteChangeDetailsOut,
    RouteChangeRunOut,
    SaleTypeBucketOut,
    TouchpointCreate,
    TouchpointOut,
    TransactionAnalyticsOut,
    TransactionMonthOut,
)
from app.schemas.client_tiers import (
    ClientTierDetailOut,
    HeldOutCountOut,
    SegmentCountOut,
    TierCountOut,
    TierSummaryOut,
)
from app.services.active_clients import (
    ActiveClientNotFound,
    ActiveClientProfile,
    deposit_percentile,
    get_active_client_profile,
    list_active_roster,
    list_interactions,
    list_route_change_history,
    list_transactions,
    record_interaction,
    route_change_details,
    transaction_analytics,
)
from app.services.briefing import deposit_trend_band_of
from app.services.client_ai_actions import AiActionNotFound, AiActionNotOpen
from app.services.client_tiers import TierSummary, summarize_client_tiers
from app.services.client_touchpoints import (
    TouchpointClientNotFound,
    TouchpointDateInPast,
    TouchpointNotFound,
    add_touchpoint,
    list_open_touchpoints,
    remove_touchpoint,
)
from app.services.fund_names import short_fund_name

router = APIRouter(
    prefix="/active-clients",
    tags=["active-clients"],
    dependencies=[Depends(get_access)],
)


@router.get("", response_model=Page[ActiveClientRosterLineOut])
def get_roster(
    client_id: int | None = None,
    risk_band: str | None = None,
    route: str | None = None,
    segment: str | None = None,
    tier: str | None = None,
    fa_id: str | None = None,
    unassigned: bool = False,
    worklist_only: bool = False,
    sort: Literal["client_id", "risk_score", "fund_at_risk"] = "client_id",
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
    access: Access = Depends(get_access),
) -> Page[ActiveClientRosterLineOut]:
    try:
        rows, next_cursor = list_active_roster(
            session,
            client_id=client_id,
            risk_band=risk_band,
            route=route,
            segment=segment,
            tier=tier,
            fa_id=book_fa_id(access, fa_id),
            unassigned=unassigned and access.sees_every_book,
            worklist_only=worklist_only,
            sort=sort,
            cursor=cursor,
            limit=limit,
        )
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(
        items=[ActiveClientRosterLineOut.model_validate(r) for r in rows], next_cursor=next_cursor
    )


def _to_tier_summary_out(summary: TierSummary) -> TierSummaryOut:
    tiers = [
        TierCountOut(
            tier=tier,
            tier_label=TIER_PLAYBOOK[tier].label,
            segment=SEGMENT_BY_TIER[tier],
            segment_label=SEGMENT_PLAYBOOK[SEGMENT_BY_TIER[tier]].label,
            tier_goal=TIER_PLAYBOOK[tier].goal,
            tier_action=TIER_PLAYBOOK[tier].fa_action,
            clients=summary.active_clients_by_tier[tier],
        )
        for tier in ACTIVE_TIERS_IN_DISPLAY_ORDER
    ]
    segments = [
        SegmentCountOut(
            segment=segment,
            segment_label=segment_playbook.label,
            segment_goal=segment_playbook.goal,
            clients=sum(row.clients for row in tiers if row.segment == segment),
        )
        for segment, segment_playbook in SEGMENT_PLAYBOOK.items()
    ]
    held_out = [
        HeldOutCountOut(
            tier=NO_TRANSACTIONS_IN_REVIEW_PERIOD,
            tier_label=TIER_PLAYBOOK[NO_TRANSACTIONS_IN_REVIEW_PERIOD].label,
            clients=summary.active_clients_with_no_transactions,
        ),
        HeldOutCountOut(
            tier=BELOW_CAPITAL_PRESERVATION_THRESHOLD,
            tier_label=TIER_PLAYBOOK[BELOW_CAPITAL_PRESERVATION_THRESHOLD].label,
            clients=summary.inactive_clients_below_threshold,
        ),
    ]
    return TierSummaryOut(
        active_clients=summary.active_clients,
        inactive_clients=summary.inactive_clients,
        segments=segments,
        tiers=tiers,
        held_out=held_out,
        active_clients_not_yet_tiered=summary.active_clients_not_yet_tiered,
        computed_at=summary.computed_at,
    )


@router.get("/tier-summary", response_model=TierSummaryOut)
def get_tier_summary(session: Session = Depends(get_session)) -> TierSummaryOut:
    return _to_tier_summary_out(summarize_client_tiers(session))


@router.get("/analytics/route-changes", response_model=Page[RouteChangeRunOut])
def get_route_change_history(
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[RouteChangeRunOut]:
    try:
        rows, next_cursor = list_route_change_history(session, cursor=cursor, limit=limit)
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(items=[RouteChangeRunOut.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/analytics/route-changes/details", response_model=RouteChangeDetailsOut)
def get_route_change_details(
    run_id: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=10, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> RouteChangeDetailsOut:
    try:
        found_run_id, as_of, rows, next_cursor = route_change_details(
            session, run_id=run_id, cursor=cursor, limit=limit
        )
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return RouteChangeDetailsOut(
        run_id=found_run_id,
        as_of=as_of,
        items=[RouteChangeDetailOut.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


@router.get("/analytics/transactions", response_model=TransactionAnalyticsOut)
def get_transaction_analytics(
    months: int = Query(default=12, ge=1, le=36),
    session: Session = Depends(get_session),
) -> TransactionAnalyticsOut:
    analytics = transaction_analytics(session, months=months)
    return TransactionAnalyticsOut(
        by_month=[
            TransactionMonthOut(
                month=month, txn_type=txn_type, count=count, total_amount=total, avg_fees=avg_fees
            )
            for month, txn_type, count, total, avg_fees in analytics.by_month
        ],
        by_sale_type=[
            SaleTypeBucketOut(sale_type=sale_type, count=count, total_amount=total)
            for sale_type, count, total in analytics.by_sale_type
        ],
    )


@router.post(
    "/{client_id}/{unit_fund_id}/interactions",
    response_model=InteractionOut,
    status_code=status.HTTP_201_CREATED,
)
def post_interaction(
    client_id: int,
    unit_fund_id: int,
    body: InteractionCreate,
    access: Access = Depends(require_client_in_book),
    session: Session = Depends(get_session),
) -> InteractionOut:
    try:
        row = record_interaction(
            session,
            client_id,
            unit_fund_id,
            type=body.type,
            note=body.note,
            reviewer_id=access.email,
            on_behalf_of=access.on_behalf_of,
            proposal_id=body.proposal_id,
            outcome=body.outcome,
            follow_up_on=body.follow_up_on,
            touchpoint_id=body.touchpoint_id,
        )
    except ActiveClientNotFound:
        raise HTTPException(status_code=404, detail="active client-fund not found") from None
    except TouchpointNotFound:
        raise HTTPException(status_code=404, detail="no open touchpoint with that id") from None
    except TouchpointDateInPast:
        raise HTTPException(status_code=422, detail="the follow-up day is in the past") from None
    except AiActionNotFound:
        raise HTTPException(
            status_code=404, detail="no AI Action from that proposal for this client"
        ) from None
    except AiActionNotOpen:
        raise HTTPException(status_code=409, detail="that AI Action is no longer open") from None
    return InteractionOut.model_validate(row)


@router.get(
    "/{client_id}/{unit_fund_id}/interactions",
    response_model=list[InteractionOut],
    dependencies=[Depends(require_client_in_book)],
)
def get_interactions(
    client_id: int,
    unit_fund_id: int,
    since: datetime | None = Query(
        default=None, description="Only interactions logged at or after this time."
    ),
    session: Session = Depends(get_session),
) -> list[InteractionOut]:
    rows = list_interactions(session, client_id, unit_fund_id, since=since)
    return [InteractionOut.model_validate(r) for r in rows]


@router.get(
    "/{client_id}/{unit_fund_id}/touchpoints",
    response_model=list[TouchpointOut],
    dependencies=[Depends(require_client_in_book)],
)
def get_touchpoints(
    client_id: int, unit_fund_id: int, session: Session = Depends(get_session)
) -> list[TouchpointOut]:
    rows = list_open_touchpoints(session, client_id, unit_fund_id)
    return [TouchpointOut.model_validate(r) for r in rows]


@router.post(
    "/{client_id}/{unit_fund_id}/touchpoints",
    response_model=TouchpointOut,
    status_code=status.HTTP_201_CREATED,
)
def post_touchpoint(
    client_id: int,
    unit_fund_id: int,
    body: TouchpointCreate,
    access: Access = Depends(require_client_in_book),
    session: Session = Depends(get_session),
) -> TouchpointOut:
    try:
        row = add_touchpoint(
            session,
            client_id,
            unit_fund_id,
            actor=access.email,
            on_behalf_of=access.on_behalf_of,
            kind=body.kind,
            title=body.title,
            due_date=body.due_date,
            due_time=body.due_time,
            minutes=body.minutes,
        )
    except TouchpointClientNotFound:
        raise HTTPException(status_code=404, detail="active client-fund not found") from None
    except TouchpointDateInPast:
        raise HTTPException(status_code=422, detail="the day is in the past") from None
    return TouchpointOut.model_validate(row)


@router.delete(
    "/{client_id}/{unit_fund_id}/touchpoints/{touchpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_touchpoint(
    client_id: int,
    unit_fund_id: int,
    touchpoint_id: int,
    access: Access = Depends(require_client_in_book),
    session: Session = Depends(get_session),
) -> None:
    try:
        remove_touchpoint(
            session,
            client_id,
            unit_fund_id,
            touchpoint_id,
            actor=access.email,
            on_behalf_of=access.on_behalf_of,
        )
    except TouchpointClientNotFound:
        raise HTTPException(status_code=404, detail="active client-fund not found") from None
    except TouchpointNotFound:
        raise HTTPException(status_code=404, detail="no open touchpoint with that id") from None


@router.get(
    "/{client_id}/{unit_fund_id}/transactions",
    response_model=list[ActiveTransactionOut],
    dependencies=[Depends(require_client_in_book)],
)
def get_transactions(
    client_id: int,
    unit_fund_id: int,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> list[ActiveTransactionOut]:
    rows = list_transactions(session, client_id, unit_fund_id, limit=limit)
    return [ActiveTransactionOut.model_validate(r) for r in rows]


@router.get(
    "/{client_id}/{unit_fund_id}/deposit-percentile",
    response_model=DepositPercentileOut,
    dependencies=[Depends(require_client_in_book)],
)
def get_deposit_percentile(
    client_id: int, unit_fund_id: int, session: Session = Depends(get_session)
) -> DepositPercentileOut:
    try:
        result = deposit_percentile(session, client_id, unit_fund_id)
    except ActiveClientNotFound:
        raise HTTPException(status_code=404, detail="active client not found") from None
    return DepositPercentileOut(
        total_deposits=result.total_deposits,
        book_size=result.book_size,
        rank=result.rank,
        percentile=result.percentile,
        deposit_count_capped=result.deposit_count_capped,
    )


def _to_tier_detail_out(client_tier: ClientTier | None) -> ClientTierDetailOut:
    if client_tier is None:
        return ClientTierDetailOut()
    tier_playbook = TIER_PLAYBOOK.get(client_tier.tier)
    return ClientTierDetailOut(
        **tier_tag_fields(client_tier.tier, client_tier.segment),
        success_measure=tier_playbook.success_measure if tier_playbook else None,
        purchased_recently=client_tier.purchased_recently,
        real_withdrawer=client_tier.real_withdrawer,
        computed_at=client_tier.computed_at,
    )


def _to_profile_out(profile: ActiveClientProfile) -> ActiveClientProfileOut:
    active = profile.active
    risk = profile.risk
    return ActiveClientProfileOut(
        identity=ActiveClientIdentityOut(
            client_id=active.client_id,
            unit_fund_id=active.unit_fund_id,
            client_code=active.client_code,
            fund_name=profile.fund_name,
            fund_short_name=short_fund_name(profile.fund_name),
            balance=active.balance,
            deposit_count_capped=active.deposit_count_capped,
            withdrawal_history_hidden=active.withdrawal_history_hidden,
        ),
        bands=ActiveClientBandsOut(
            recency_band=risk.recency_band if risk else None,
            balance_tier=risk.balance_tier if risk else None,
            value_tier=risk.value_tier if risk else None,
            pattern_is_reliable=risk.pattern_is_reliable if risk else False,
            risk_score=risk.risk_score if risk else None,
            risk_band=risk.risk_band if risk else None,
            risk_reasons=risk.risk_reasons if risk else None,
            risk_reason_tags=(
                advisor_signal_tags(risk, profile.near_preservation_line) if risk else []
            ),
            route=risk.route if risk else None,
            fund_at_risk=risk.fund_at_risk if risk else None,
            primary_signal_magnitude=profile.primary_signal_magnitude,
            deposit_trend_band=deposit_trend_band_of(active.deposit_trend),
        ),
        risk_history=[
            ActiveClientRiskHistoryEntryOut(
                run_id=h.run_id,
                risk_score=h.risk_score,
                risk_band=h.risk_band,
                risk_reasons=h.risk_reasons,
                risk_reason_tags=fired_signal_tags(h),
                route=h.route,
                created_at=h.created_at,
            )
            for h in profile.risk_history
        ],
        complaints=[
            ActiveClientComplaintOut(
                id=c.id,
                opened_at=c.opened_at,
                closed_at=c.closed_at,
                status=c.status,
                category=c.category,
                channel=c.channel,
            )
            for c in profile.complaints
        ],
        interactions=[InteractionOut.model_validate(i) for i in profile.interactions],
        transactions=[ActiveTransactionOut.model_validate(t) for t in profile.transactions],
        segment_and_tier=_to_tier_detail_out(profile.client_tier),
        ai_action=AiActionOut.model_validate(profile.ai_action) if profile.ai_action else None,
        review_activity=(
            ReviewPeriodActivityOut(
                review_period_days=profile.review_activity.review_period_days,
                deposits=profile.review_activity.deposits,
                withdrawals=profile.review_activity.withdrawals,
            )
            if profile.review_activity
            else None
        ),
    )


@router.get(
    "/{client_id}/{unit_fund_id}/profile",
    response_model=ActiveClientProfileOut,
    dependencies=[Depends(require_client_in_book)],
)
def get_profile(
    client_id: int, unit_fund_id: int, session: Session = Depends(get_session)
) -> ActiveClientProfileOut:
    try:
        profile = get_active_client_profile(session, client_id, unit_fund_id)
    except ActiveClientNotFound:
        raise HTTPException(status_code=404, detail="active client not found") from None
    return _to_profile_out(profile)
