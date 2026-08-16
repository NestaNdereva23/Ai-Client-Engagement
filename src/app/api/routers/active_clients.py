"""Active-client console endpoints: the digest/risk population's own
counterpart to app.api.routers.clients, keyed by (client_id, unit_fund_id)
rather than by client_id alone.

A distinct route prefix and a distinct profile endpoint from the dormant
clients.py router, on purpose: that router reads client_fund, this one
reads active_client_fund, and the two populations must never share a route
that could return the wrong record for a coincidentally matching id.

POST .../interactions is the one write path here, gated the same way the
review/template decide endpoints are: the X-Reviewer-Key stopgap
(app.api.reviewer_auth) resolves the caller to a reviewer_id server-side,
never a self-reported one, and every write is audited. Every GET read here
carries no PII and stays ungated, the same as GET /clients/{id}/profile.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.risk.signals import fired_signal_tags
from app.schemas.active_clients import (
    ActiveClientBandsOut,
    ActiveClientComplaintOut,
    ActiveClientIdentityOut,
    ActiveClientProfileOut,
    ActiveClientRiskHistoryEntryOut,
    ActiveClientRosterLineOut,
    ActiveTransactionOut,
    ContributionPercentileOut,
    InteractionCreate,
    InteractionOut,
    RouteChangeDetailOut,
    RouteChangeDetailsOut,
    RouteChangeRunOut,
    SaleTypeBucketOut,
    TransactionAnalyticsOut,
    TransactionMonthOut,
)
from app.services.active_clients import (
    ActiveClientNotFound,
    ActiveClientProfile,
    contribution_percentile,
    get_active_client_profile,
    list_active_roster,
    list_interactions,
    list_route_change_history,
    list_transactions,
    record_interaction,
    route_change_details,
    transaction_analytics,
)

router = APIRouter(prefix="/active-clients", tags=["active-clients"])


@router.get("", response_model=Page[ActiveClientRosterLineOut])
def get_roster(
    client_id: int | None = None,
    risk_band: str | None = None,
    route: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[ActiveClientRosterLineOut]:
    """The paginated active-book roster: every active_client_fund row, with
    its current risk bands where a nightly run has scored it. Carries no
    PII, so it is not gated behind the reviewer key. client_id is an exact
    match, the same "Find by ID" box GET /clients already exposes.
    """
    try:
        rows, next_cursor = list_active_roster(
            session,
            client_id=client_id,
            risk_band=risk_band,
            route=route,
            cursor=cursor,
            limit=limit,
        )
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(
        items=[ActiveClientRosterLineOut.model_validate(r) for r in rows], next_cursor=next_cursor
    )


@router.get("/analytics/route-changes", response_model=Page[RouteChangeRunOut])
def get_route_change_history(
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[RouteChangeRunOut]:
    """Route-churn across nightly risk runs, newest first: how many
    client-funds moved to a different route each run, alongside that run's
    coverage and route mix. Carries no PII, so it is not gated behind the
    reviewer key.
    """
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
    """The client-level route moves behind one nightly run's routes_changed
    count -- the latest run with any changes by default, or a specific
    run_id. Capped at `limit` (10 by default), paginated via next_cursor.
    Carries no PII, so it is not gated behind the reviewer key.
    """
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
    """Book-wide purchase/sale volume by month over the trailing `months`
    months, plus a sale_type breakdown among sales. Carries no PII, so it
    is not gated behind the reviewer key.
    """
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
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> InteractionOut:
    """Log a call, a snooze, or a dismiss against one digest line.

    Requires the X-Reviewer-Key header; the log entry is recorded under
    the reviewer_id that key resolved to. 404s when the client-fund isn't
    in the active book at all.
    """
    try:
        row = record_interaction(
            session,
            client_id,
            unit_fund_id,
            type=body.type,
            note=body.note,
            reviewer_id=reviewer_id,
        )
    except ActiveClientNotFound:
        raise HTTPException(status_code=404, detail="active client-fund not found") from None
    return InteractionOut.model_validate(row)


@router.get("/{client_id}/{unit_fund_id}/interactions", response_model=list[InteractionOut])
def get_interactions(
    client_id: int,
    unit_fund_id: int,
    since: datetime | None = Query(
        default=None, description="Only interactions logged at or after this time."
    ),
    session: Session = Depends(get_session),
) -> list[InteractionOut]:
    """This client-fund's logged interactions, most recent first. Carries
    no PII -- only what an FA already did and when -- so it is not gated
    behind the reviewer key.
    """
    rows = list_interactions(session, client_id, unit_fund_id, since=since)
    return [InteractionOut.model_validate(r) for r in rows]


@router.get("/{client_id}/{unit_fund_id}/transactions", response_model=list[ActiveTransactionOut])
def get_transactions(
    client_id: int,
    unit_fund_id: int,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> list[ActiveTransactionOut]:
    """This client-fund's observed purchases and sales, most recent first.
    Carries no PII, so it is not gated behind the reviewer key. Not
    a claim of full lifetime history -- see
    ActiveClientProfileOut.identity.purchases_censored /
    redemption_history_blind.
    """
    rows = list_transactions(session, client_id, unit_fund_id, limit=limit)
    return [ActiveTransactionOut.model_validate(r) for r in rows]


@router.get(
    "/{client_id}/{unit_fund_id}/contribution-percentile",
    response_model=ContributionPercentileOut,
)
def get_contribution_percentile(
    client_id: int, unit_fund_id: int, session: Session = Depends(get_session)
) -> ContributionPercentileOut:
    """Where this client-fund's observed lifetime purchase total ranks
    against the whole active book. Carries no PII, so it is not gated
    behind the reviewer key. 404s when the client-fund isn't in the active
    book at all.
    """
    try:
        result = contribution_percentile(session, client_id, unit_fund_id)
    except ActiveClientNotFound:
        raise HTTPException(status_code=404, detail="active client not found") from None
    return ContributionPercentileOut(
        total_contribution=result.total_contribution,
        book_size=result.book_size,
        rank=result.rank,
        percentile=result.percentile,
        purchases_censored=result.purchases_censored,
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
            balance=active.balance,
            purchases_censored=active.purchases_censored,
            redemption_history_blind=active.redemption_history_blind,
        ),
        bands=ActiveClientBandsOut(
            recency_band=risk.recency_band if risk else None,
            balance_tier=risk.balance_tier if risk else None,
            value_tier=risk.value_tier if risk else None,
            credible_rhythm=risk.credible_rhythm if risk else False,
            risk_score=risk.risk_score if risk else None,
            risk_band=risk.risk_band if risk else None,
            risk_reasons=risk.risk_reasons if risk else None,
            risk_reason_tags=fired_signal_tags(risk) if risk else [],
            route=risk.route if risk else None,
            aum_at_risk=risk.aum_at_risk if risk else None,
            primary_signal_magnitude=profile.primary_signal_magnitude,
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
    )


@router.get("/{client_id}/{unit_fund_id}/profile", response_model=ActiveClientProfileOut)
def get_profile(
    client_id: int, unit_fund_id: int, session: Session = Depends(get_session)
) -> ActiveClientProfileOut:
    """Identity, current bands, risk-signal history, and complaint/
    interaction/transaction history for one active-client-fund
    relationship. No name, the same boundary app.api.routers.clients draws
    for the dormant population -- safe to call with no gate at all.
    """
    try:
        profile = get_active_client_profile(session, client_id, unit_fund_id)
    except ActiveClientNotFound:
        raise HTTPException(status_code=404, detail="active client not found") from None
    return _to_profile_out(profile)
