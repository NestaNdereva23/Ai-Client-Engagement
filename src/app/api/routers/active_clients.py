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
never a self-reported one, and every write is audited. The two GET reads
carry no PII and stay ungated, the same as GET /clients/{id}/profile.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.schemas.active_clients import (
    ActiveClientBandsOut,
    ActiveClientComplaintOut,
    ActiveClientIdentityOut,
    ActiveClientProfileOut,
    ActiveClientRiskHistoryEntryOut,
    InteractionCreate,
    InteractionOut,
)
from app.services.active_clients import (
    ActiveClientNotFound,
    ActiveClientProfile,
    get_active_client_profile,
    list_interactions,
    record_interaction,
)

router = APIRouter(prefix="/active-clients", tags=["active-clients"])


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


def _to_profile_out(profile: ActiveClientProfile) -> ActiveClientProfileOut:
    active = profile.active
    risk = profile.risk
    return ActiveClientProfileOut(
        identity=ActiveClientIdentityOut(
            client_id=active.client_id,
            unit_fund_id=active.unit_fund_id,
            client_code=active.client_code,
            fund_name=profile.fund_name,
        ),
        bands=ActiveClientBandsOut(
            recency_band=risk.recency_band if risk else None,
            balance_tier=risk.balance_tier if risk else None,
            value_tier=risk.value_tier if risk else None,
            credible_rhythm=risk.credible_rhythm if risk else False,
            risk_score=risk.risk_score if risk else None,
            risk_band=risk.risk_band if risk else None,
            risk_reasons=risk.risk_reasons if risk else None,
            route=risk.route if risk else None,
            aum_at_risk=risk.aum_at_risk if risk else None,
        ),
        risk_history=[
            ActiveClientRiskHistoryEntryOut(
                run_id=h.run_id,
                risk_score=h.risk_score,
                risk_band=h.risk_band,
                risk_reasons=h.risk_reasons,
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
    )


@router.get("/{client_id}/{unit_fund_id}/profile", response_model=ActiveClientProfileOut)
def get_profile(
    client_id: int, unit_fund_id: int, session: Session = Depends(get_session)
) -> ActiveClientProfileOut:
    """Identity, current bands, risk-signal history, and complaint/
    interaction history for one active-client-fund relationship. No name,
    the same boundary app.api.routers.clients draws for the dormant
    population -- safe to call with no gate at all.
    """
    try:
        profile = get_active_client_profile(session, client_id, unit_fund_id)
    except ActiveClientNotFound:
        raise HTTPException(status_code=404, detail="active client not found") from None
    return _to_profile_out(profile)
