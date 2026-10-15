from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.access import Access, book_fa_id, get_access
from app.api.errors import ApiError
from app.briefing.book import render_book_briefing
from app.config import get_settings
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor
from app.privacy.llm_client import get_briefing_llm_client
from app.risk.routing import ROUTE_FA_ACTION
from app.routing.tier_playbook import SEGMENT_PLAYBOOK, TIER_PLAYBOOK, tier_tag_fields
from app.routing.tiers import (
    BELOW_CAPITAL_PRESERVATION_THRESHOLD,
    NO_TRANSACTIONS_IN_REVIEW_PERIOD,
    SEGMENT_BY_TIER,
)
from app.schemas.fa_action import (
    FaActionBookOut,
    FaActionBriefingOut,
    FaActionClientOut,
    FaActionGroupOut,
    FaActionHeldOutOut,
)
from app.services.book_briefing import book_facts_for, get_book_narrative
from app.services.briefing import NarrativeDisabled
from app.services.fa_action import (
    FaActionBook,
    NotAWorklistTier,
    load_fa_action_book,
)

router = APIRouter(prefix="/fa-action", tags=["fa-action"], dependencies=[Depends(get_access)])


def _narrative_disabled() -> ApiError:
    return ApiError(
        status_code=404,
        code="narrative_disabled",
        detail="AI-narrated briefings are not enabled",
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


@router.get("/{fa_id}/briefing", response_model=FaActionBriefingOut)
def get_fa_action_briefing(
    fa_id: str,
    access: Access = Depends(get_access),
    session: Session = Depends(get_session),
) -> FaActionBriefingOut:
    fa_id = book_fa_id(access, fa_id)
    return FaActionBriefingOut(
        fa_id=fa_id, text=render_book_briefing(book_facts_for(session, fa_id))
    )


@router.get("/{fa_id}/briefing/narrative", response_model=FaActionBriefingOut)
def get_fa_action_briefing_narrative(
    fa_id: str,
    access: Access = Depends(get_access),
    session: Session = Depends(get_session),
) -> FaActionBriefingOut:
    fa_id = book_fa_id(access, fa_id)
    settings = get_settings()
    if not settings.ai_briefing_enabled:
        raise _narrative_disabled()
    try:
        result = get_book_narrative(session, fa_id, llm_client=get_briefing_llm_client(settings))
    except NarrativeDisabled:
        raise _narrative_disabled() from None
    return FaActionBriefingOut(fa_id=fa_id, text=result.text, mode=result.mode)


@router.get("/{fa_id}", response_model=FaActionBookOut)
def get_fa_action_book(
    fa_id: str,
    tier: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    access: Access = Depends(get_access),
    session: Session = Depends(get_session),
) -> FaActionBookOut:
    if cursor is not None and tier is None:
        raise HTTPException(status_code=400, detail="a cursor needs a tier")
    fa_id = book_fa_id(access, fa_id)
    try:
        book = load_fa_action_book(session, fa_id, tier=tier, cursor=cursor, limit=limit)
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    except NotAWorklistTier:
        raise HTTPException(
            status_code=400, detail="not a tier on the FA Action worklist"
        ) from None
    return _to_book_out(book)
