from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor
from app.risk.routing import ROUTE_FA_ACTION
from app.routing.tier_playbook import SEGMENT_PLAYBOOK, TIER_PLAYBOOK, tier_tag_fields
from app.routing.tiers import (
    BELOW_CAPITAL_PRESERVATION_THRESHOLD,
    NO_TRANSACTIONS_IN_REVIEW_PERIOD,
    SEGMENT_BY_TIER,
)
from app.schemas.fa_action import (
    FaActionBookOut,
    FaActionClientOut,
    FaActionGroupOut,
    FaActionHeldOutOut,
)
from app.services.fa_action import (
    FaActionBook,
    NotAWorklistTier,
    load_fa_action_book,
)

router = APIRouter(
    prefix="/fa-action", tags=["fa-action"], dependencies=[Depends(get_current_reviewer_id)]
)


def _to_book_out(book: FaActionBook) -> FaActionBookOut:
    groups = [
        FaActionGroupOut(
            **tier_tag_fields(group.tier, SEGMENT_BY_TIER[group.tier]),
            segment_goal=SEGMENT_PLAYBOOK[SEGMENT_BY_TIER[group.tier]].goal,
            segment_definition=SEGMENT_PLAYBOOK[SEGMENT_BY_TIER[group.tier]].definition,
            success_measure=TIER_PLAYBOOK[group.tier].success_measure,
            clients=group.totals.clients,
            balance_kes=group.totals.balance_kes,
            fund_at_risk_kes=group.totals.fund_at_risk_kes,
            items=[FaActionClientOut(**vars(client)) for client in group.items],
            next_cursor=group.next_cursor,
        )
        for group in book.groups
    ]
    held_out = [
        FaActionHeldOutOut(
            tier=tier,
            tier_label=TIER_PLAYBOOK[tier].label,
            clients=totals.clients,
            balance_kes=totals.balance_kes,
        )
        for tier, totals in (
            (NO_TRANSACTIONS_IN_REVIEW_PERIOD, book.no_transactions),
            (BELOW_CAPITAL_PRESERVATION_THRESHOLD, book.below_threshold),
        )
    ]
    return FaActionBookOut(
        route=ROUTE_FA_ACTION,
        fa_id=book.fa_id,
        fa_name=book.fa_name,
        assigned_clients=book.assigned_clients,
        computed_at=book.computed_at,
        groups=groups,
        held_out=held_out,
        not_yet_tiered=book.not_yet_tiered.clients,
        not_yet_tiered_balance_kes=book.not_yet_tiered.balance_kes,
    )


@router.get("/{fa_id}", response_model=FaActionBookOut)
def get_fa_action_book(
    fa_id: str,
    tier: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> FaActionBookOut:
    if cursor is not None and tier is None:
        raise HTTPException(status_code=400, detail="a cursor needs a tier")
    try:
        book = load_fa_action_book(session, fa_id, tier=tier, cursor=cursor, limit=limit)
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    except NotAWorklistTier:
        raise HTTPException(
            status_code=400, detail="not a tier on the FA Action worklist"
        ) from None
    return _to_book_out(book)
