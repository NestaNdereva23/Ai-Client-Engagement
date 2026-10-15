from __future__ import annotations

from calendar import monthrange
from collections.abc import Sequence
from datetime import date

from sqlalchemy import and_, exists, func, literal_column, select
from sqlalchemy.orm import Session

from app.briefing.book import BookFacts, MonthEndPattern, TierLine, render_book_briefing
from app.briefing.book_narrative import draft_book_narrative
from app.briefing.narrative import NarrativeResult
from app.config import get_settings
from app.db.models.active_clients import ActiveTransaction
from app.db.models.fa_assignment import FaAssignment
from app.privacy.llm_client import LLMClient
from app.routing.tier_playbook import TIER_PLAYBOOK
from app.routing.tiers import (
    ACTIVE_TIERS_IN_DISPLAY_ORDER,
    BELOW_CAPITAL_PRESERVATION_THRESHOLD,
    NO_TRANSACTIONS_IN_REVIEW_PERIOD,
    SEGMENT_BY_TIER,
)
from app.services.briefing import NarrativeDisabled, briefing_boundary_audit_sink
from app.services.client_touchpoints import today_in_nairobi
from app.services.fa_action import TierTotals, tier_totals_for

LOOKBACK_MONTHS = 6
MIN_MONTHS_WITH_LATE_TOPUP = 3
LAST_DAYS_OF_MONTH = 7
SHOW_IN_LAST_DAYS = 10


def _first_of_month(today: date, months_back: int) -> date:
    index = today.year * 12 + today.month - 1 - months_back
    return date(index // 12, index % 12 + 1, 1)


def month_end_patterns(
    session: Session, fa_ids: Sequence[str], today: date
) -> dict[str, MonthEndPattern]:
    days_left = monthrange(today.year, today.month)[1] - today.day
    if not fa_ids or days_left >= SHOW_IN_LAST_DAYS:
        return {}

    this_month = _first_of_month(today, 0)
    month_start = func.date_trunc("month", ActiveTransaction.txn_date)
    in_last_days = ActiveTransaction.txn_date >= month_start + literal_column(
        f"interval '1 month' - interval '{LAST_DAYS_OF_MONTH} days'"
    )
    late_months = (
        select(FaAssignment.fa_id, FaAssignment.client_id, month_start.label("month"))
        .select_from(FaAssignment)
        .join(
            ActiveTransaction,
            and_(
                ActiveTransaction.client_id == FaAssignment.client_id,
                ActiveTransaction.unit_fund_id == FaAssignment.unit_fund_id,
            ),
        )
        .where(
            FaAssignment.fa_id.in_(list(fa_ids)),
            ActiveTransaction.txn_type == "purchase",
            ActiveTransaction.txn_date >= _first_of_month(today, LOOKBACK_MONTHS),
            ActiveTransaction.txn_date < this_month,
            in_last_days,
        )
        .distinct()
        .subquery()
    )
    usual = (
        select(late_months.c.fa_id, late_months.c.client_id)
        .group_by(late_months.c.fa_id, late_months.c.client_id)
        .having(func.count() >= MIN_MONTHS_WITH_LATE_TOPUP)
        .subquery()
    )
    topped_up_this_month = exists().where(
        ActiveTransaction.client_id == usual.c.client_id,
        ActiveTransaction.txn_type == "purchase",
        ActiveTransaction.txn_date >= this_month,
    )
    rows = session.execute(
        select(usual.c.fa_id, func.count(), func.count().filter(~topped_up_this_month)).group_by(
            usual.c.fa_id
        )
    ).all()
    return {
        fa_id: MonthEndPattern(usual_clients=usual_clients, not_yet_clients=not_yet)
        for fa_id, usual_clients, not_yet in rows
    }


def gather_book_facts(
    session: Session,
    fa_id: str,
    *,
    advisor_name: str | None = None,
    month_end: MonthEndPattern | None = None,
) -> BookFacts:
    totals, computed_at = tier_totals_for(session, fa_id)
    if advisor_name is None:
        advisor_name = (
            session.scalar(
                select(func.max(FaAssignment.fa_name)).where(FaAssignment.fa_id == fa_id)
            )
            or fa_id
        )
    tiers = []
    for tier in ACTIVE_TIERS_IN_DISPLAY_ORDER:
        tier_totals = totals.get(tier, TierTotals())
        tiers.append(
            TierLine(
                tier=tier,
                segment=SEGMENT_BY_TIER[tier],
                label=TIER_PLAYBOOK[tier].label,
                goal=TIER_PLAYBOOK[tier].goal,
                clients=tier_totals.clients,
                balance_kes=tier_totals.balance_kes,
                at_risk_kes=tier_totals.fund_at_risk_kes,
            )
        )
    return BookFacts(
        advisor_name=advisor_name,
        assigned_clients=sum(item.clients for item in totals.values()),
        tiers=tiers,
        held_out_clients=(
            totals.get(NO_TRANSACTIONS_IN_REVIEW_PERIOD, TierTotals()).clients
            + totals.get(BELOW_CAPITAL_PRESERVATION_THRESHOLD, TierTotals()).clients
        ),
        not_yet_tiered_clients=totals.get(None, TierTotals()).clients,
        month_end=month_end,
        computed_at=computed_at,
    )


def book_facts_for(session: Session, fa_id: str) -> BookFacts:
    month_end = month_end_patterns(session, [fa_id], today_in_nairobi()).get(fa_id)
    return gather_book_facts(session, fa_id, month_end=month_end)


def get_book_narrative(session: Session, fa_id: str, *, llm_client: LLMClient) -> NarrativeResult:
    if not get_settings().ai_briefing_enabled:
        raise NarrativeDisabled("AI-narrated briefings are not enabled")

    facts = book_facts_for(session, fa_id)
    plain = render_book_briefing(facts)
    if facts.worklist_clients == 0:
        return NarrativeResult(text=plain, mode="deterministic_fallback")

    result = draft_book_narrative(
        facts,
        llm_client,
        fallback_text=plain,
        entity_id=fa_id,
        audit=briefing_boundary_audit_sink(session, entity_type="book_briefing"),
    )
    session.commit()
    return result
