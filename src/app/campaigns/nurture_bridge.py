from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.campaigns.enrollment import enroll_cohort
from app.db.models.active_clients import ActiveClientFund, ActiveTransaction
from app.db.models.models import ClientFeatures, Clients, Funds
from app.db.models.outreach import Campaign
from app.db.models.risk import ClientRiskFeatures
from app.db.models.rules import ClientMessageIndicators
from app.risk.scoring import band_rank
from app.rules.engine import resolve
from app.rules.store import load_active_rules
from app.transform.features import PRIORITY_TIERS
from app.transform.load import upsert

AUTO_CHECKIN_CAMPAIGN_TYPE = "auto_checkin_nurture"
AUTO_CHECKIN_ANGLE = "sitting_still"

_BRIDGE_PURCHASE_DEPTH = "capped"
_TIER_URGENCY = {"T1": "high", "T2": "medium", "T3": "medium", "T4": "low"}
_RISK_BAND_TIER = {"Critical": "T2", "High": "T2", "Watch": "T3", "Low": "T4", "None": "T4"}
_DEFAULT_PRIORITY_TIER = "T3"

_CLIENT_FEATURES_UPDATE = ["active_book_auto_checkin"]
_INDICATOR_UPDATE = [
    "message_angle",
    "urgency",
    "priority_tier",
    "prompt_variant",
    "rule_id",
    "rule_name",
    "rule_version",
]


@dataclass(frozen=True)
class ClientAggregate:
    client_id: int
    client_code: str | None
    unit_fund_id: int
    balance: float
    n_deposits: int
    n_withdrawals: int
    last_purchase_date: date | None
    last_sale_date: date | None
    last_activity_date: date | None
    n_funds: int
    purchases_censored: bool
    fund_ids: tuple[int, ...]


def _aggregate_active_funds(session: Session, client_id: int) -> ClientAggregate | None:
    funds = list(
        session.scalars(
            select(ActiveClientFund).where(ActiveClientFund.client_id == client_id)
        ).all()
    )
    if not funds:
        return None
    primary = max(funds, key=lambda f: f.balance or 0.0)
    last_purchase = max(
        (f.last_deposit_date for f in funds if f.last_deposit_date is not None), default=None
    )
    last_sale = max(
        (f.last_withdrawal_slot_date for f in funds if f.last_withdrawal_slot_date is not None),
        default=None,
    )
    last_activity = max((d for d in (last_purchase, last_sale) if d is not None), default=None)
    return ClientAggregate(
        client_id=client_id,
        client_code=primary.client_code,
        unit_fund_id=primary.unit_fund_id,
        balance=sum(f.balance or 0.0 for f in funds),
        n_deposits=sum(f.n_deposits for f in funds),
        n_withdrawals=sum(f.n_withdrawals for f in funds),
        last_purchase_date=last_purchase,
        last_sale_date=last_sale,
        last_activity_date=last_activity,
        n_funds=len(funds),
        purchases_censored=any(f.deposit_count_capped for f in funds),
        fund_ids=tuple(f.unit_fund_id for f in funds),
    )


def _fund_name(session: Session, unit_fund_id: int) -> str:
    name = session.scalar(
        select(ActiveTransaction.fund_short_name)
        .where(
            ActiveTransaction.unit_fund_id == unit_fund_id,
            ActiveTransaction.fund_short_name.isnot(None),
        )
        .limit(1)
    )
    return name or f"Fund {unit_fund_id}"


def _ensure_funds(session: Session, unit_fund_ids: Sequence[int]) -> None:
    missing = {fund_id for fund_id in unit_fund_ids if session.get(Funds, fund_id) is None}
    if not missing:
        return
    rows = [
        {"unit_fund_id": fund_id, "unit_fund_name": _fund_name(session, fund_id)}
        for fund_id in missing
    ]
    stmt = pg_insert(Funds).values(rows).on_conflict_do_nothing(index_elements=["unit_fund_id"])
    session.execute(stmt)


def _insert_client_if_absent(session: Session, aggregate: ClientAggregate) -> None:
    if session.get(Clients, aggregate.client_id) is not None:
        return
    stmt = pg_insert(Clients).values(
        client_id=aggregate.client_id,
        client_code=aggregate.client_code,
        unit_fund_id=aggregate.unit_fund_id,
        balance=aggregate.balance,
        n_purchases_returned=aggregate.n_deposits,
        n_sales_returned=aggregate.n_withdrawals,
        last_purchase_date=aggregate.last_purchase_date,
        last_sale_date=aggregate.last_sale_date,
        total_purchase_amount=aggregate.balance,
        last_activity_date=aggregate.last_activity_date,
        n_funds=aggregate.n_funds,
        purchases_censored=aggregate.purchases_censored,
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["client_id"])
    session.execute(stmt)


def _priority_tier_for(session: Session, client_id: int) -> str:
    bands = list(
        session.scalars(
            select(ClientRiskFeatures.risk_band).where(ClientRiskFeatures.client_id == client_id)
        ).all()
    )
    if not bands:
        return _DEFAULT_PRIORITY_TIER
    worst = max(bands, key=band_rank)
    return _RISK_BAND_TIER.get(worst, _DEFAULT_PRIORITY_TIER)


def _upsert_client_features(session: Session, client_id: int, priority_tier: str) -> None:
    rows = [
        {
            "client_id": client_id,
            "active_book_auto_checkin": True,
            "purchase_depth": _BRIDGE_PURCHASE_DEPTH,
            "priority_tier": priority_tier,
        }
    ]
    upsert(session, ClientFeatures, rows, "client_id", _CLIENT_FEATURES_UPDATE)


def _resolve_and_upsert_indicator(
    session: Session, client_id: int, priority_tier: str, *, at: date
) -> None:
    rules = load_active_rules(session, at)
    resolution = resolve({"active_book_auto_checkin": "true"}, rules)
    urgency = resolution.urgency
    tier = resolution.priority_tier
    if tier in PRIORITY_TIERS:
        tier = priority_tier
        urgency = _TIER_URGENCY[tier]
    rows = [
        {
            "client_id": client_id,
            "message_angle": resolution.message_angle,
            "urgency": urgency,
            "priority_tier": tier,
            "prompt_variant": resolution.prompt_variant,
            "rule_id": resolution.rule_id,
            "rule_name": resolution.rule_name,
            "rule_version": resolution.version,
        }
    ]
    upsert(session, ClientMessageIndicators, rows, "client_id", _INDICATOR_UPDATE)


def _find_campaign(session: Session) -> Campaign | None:
    return session.scalar(
        select(Campaign)
        .where(Campaign.campaign_type == AUTO_CHECKIN_CAMPAIGN_TYPE, Campaign.status == "running")
        .order_by(Campaign.campaign_id)
        .limit(1)
    )


def enroll_auto_checkin_clients(
    session: Session, client_ids: Sequence[int], *, at: date | None = None
) -> list[int]:
    on = at or date.today()
    unique_ids = list(dict.fromkeys(client_ids))
    if not unique_ids:
        return []

    campaign = _find_campaign(session)
    if campaign is None:
        return []

    bridged: list[int] = []
    for client_id in unique_ids:
        aggregate = _aggregate_active_funds(session, client_id)
        if aggregate is None:
            continue
        _ensure_funds(session, aggregate.fund_ids)
        _insert_client_if_absent(session, aggregate)
        priority_tier = _priority_tier_for(session, client_id)
        _upsert_client_features(session, client_id, priority_tier)
        _resolve_and_upsert_indicator(session, client_id, priority_tier, at=on)
        bridged.append(client_id)

    if not bridged:
        return []

    enroll_cohort(session, campaign_id=campaign.campaign_id, client_ids=bridged)
    record_audit(
        session,
        entity_type="enrollment",
        action="auto_checkin_sync",
        entity_id=str(campaign.campaign_id),
        detail={"client_ids": bridged},
    )
    return bridged
