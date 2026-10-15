from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.db.models.active_clients import ActiveClientFund
from app.db.models.fa_assignment import FaAssignment
from app.db.models.risk import ClientRiskFeatures
from app.db.models.routing import ClientTier
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_id_cursor, encode_id_cursor
from app.routing.tiers import (
    ACTIVE_TIERS_IN_DISPLAY_ORDER,
    BELOW_CAPITAL_PRESERVATION_THRESHOLD,
    NO_TRANSACTIONS_IN_REVIEW_PERIOD,
)
from app.workers.fa_assignment import FEED_SOURCE


class NotAWorklistTier(ValueError):
    pass


@dataclass(frozen=True)
class FaActionClient:
    client_id: int
    client_code: str | None
    funds_held: int
    total_balance_kes: float | None
    owner_matched_by_email: bool


@dataclass(frozen=True)
class TierTotals:
    clients: int = 0
    balance_kes: float = 0.0
    fund_at_risk_kes: float = 0.0


@dataclass(frozen=True)
class FaActionGroup:
    tier: str
    totals: TierTotals
    items: list[FaActionClient]
    next_cursor: str | None


@dataclass(frozen=True)
class FaActionBook:
    fa_id: str
    fa_name: str | None
    assigned_clients: int
    computed_at: datetime | None
    groups: list[FaActionGroup]
    no_transactions: TierTotals
    below_threshold: TierTotals
    not_yet_tiered: TierTotals


def _funds_assigned_to(fa_id: str, *columns):
    return (
        select(*columns)
        .select_from(FaAssignment)
        .join(
            ActiveClientFund,
            and_(
                ActiveClientFund.client_id == FaAssignment.client_id,
                ActiveClientFund.unit_fund_id == FaAssignment.unit_fund_id,
            ),
        )
        .join(ClientTier, ClientTier.client_id == FaAssignment.client_id, isouter=True)
        .where(FaAssignment.fa_id == fa_id)
    )


def tier_totals_for(
    session: Session, fa_id: str
) -> tuple[dict[str | None, TierTotals], datetime | None]:
    rows = session.execute(
        _funds_assigned_to(
            fa_id,
            ClientTier.tier,
            func.count(func.distinct(FaAssignment.client_id)),
            func.coalesce(func.sum(ActiveClientFund.balance), 0.0),
            func.coalesce(func.sum(ClientRiskFeatures.fund_at_risk), 0.0),
            func.max(ClientTier.computed_at),
        )
        .join(
            ClientRiskFeatures,
            and_(
                ClientRiskFeatures.client_id == FaAssignment.client_id,
                ClientRiskFeatures.unit_fund_id == FaAssignment.unit_fund_id,
            ),
            isouter=True,
        )
        .group_by(ClientTier.tier)
    ).all()
    totals = {
        tier: TierTotals(clients=clients, balance_kes=balance, fund_at_risk_kes=at_risk)
        for tier, clients, balance, at_risk, _computed_at in rows
    }
    computed_times = [computed_at for *_, computed_at in rows if computed_at is not None]
    return totals, max(computed_times, default=None)


def _group_page(
    session: Session, fa_id: str, tier: str, cursor: str | None, limit: int
) -> tuple[list[FaActionClient], str | None]:
    query = (
        _funds_assigned_to(
            fa_id,
            ClientTier.client_id,
            func.max(ActiveClientFund.client_code).label("client_code"),
            func.count().label("funds_held"),
            func.sum(ActiveClientFund.balance).label("total_balance_kes"),
            func.bool_and(FaAssignment.source == FEED_SOURCE).label("owner_matched_by_email"),
        )
        .where(ClientTier.tier == tier)
        .group_by(ClientTier.client_id)
        .order_by(ClientTier.client_id)
        .limit(limit + 1)
    )
    if cursor is not None:
        query = query.where(ClientTier.client_id > decode_id_cursor(cursor))

    rows = session.execute(query).all()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_id_cursor(rows[-1].client_id)
    clients = [
        FaActionClient(
            client_id=row.client_id,
            client_code=row.client_code,
            funds_held=row.funds_held,
            total_balance_kes=row.total_balance_kes,
            owner_matched_by_email=row.owner_matched_by_email,
        )
        for row in rows
    ]
    return clients, next_cursor


def load_fa_action_book(
    session: Session,
    fa_id: str,
    *,
    tier: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> FaActionBook:
    if tier is not None and tier not in ACTIVE_TIERS_IN_DISPLAY_ORDER:
        raise NotAWorklistTier(tier)

    limit = clamp_limit(limit)
    totals_by_tier, computed_at = tier_totals_for(session, fa_id)

    groups = []
    for group_tier in (tier,) if tier else ACTIVE_TIERS_IN_DISPLAY_ORDER:
        totals = totals_by_tier.get(group_tier, TierTotals())
        items, next_cursor = (
            _group_page(session, fa_id, group_tier, cursor if tier else None, limit)
            if totals.clients
            else ([], None)
        )
        groups.append(
            FaActionGroup(tier=group_tier, totals=totals, items=items, next_cursor=next_cursor)
        )

    fa_name = session.scalar(
        select(func.max(FaAssignment.fa_name)).where(FaAssignment.fa_id == fa_id)
    )
    return FaActionBook(
        fa_id=fa_id,
        fa_name=fa_name,
        assigned_clients=sum(totals.clients for totals in totals_by_tier.values()),
        computed_at=computed_at,
        groups=groups,
        no_transactions=totals_by_tier.get(NO_TRANSACTIONS_IN_REVIEW_PERIOD, TierTotals()),
        below_threshold=totals_by_tier.get(BELOW_CAPITAL_PRESERVATION_THRESHOLD, TierTotals()),
        not_yet_tiered=totals_by_tier.get(None, TierTotals()),
    )
