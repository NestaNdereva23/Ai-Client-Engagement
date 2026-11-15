from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.bulk import copy_upsert
from app.db.models.routing import ClientTier, ClientTierThresholds
from app.transform.active_features import ActiveFeatureMeasures

logger = structlog.get_logger(__name__)

NO_TRANSACTIONS_IN_REVIEW_PERIOD = "no_transactions_in_review_period"
BELOW_CAPITAL_PRESERVATION_THRESHOLD = "below_capital_preservation_threshold"

SEGMENT_ACTIVE_ACTIVE = "active_active"
SEGMENT_ACTIVE_DORMANT = "active_dormant"

TIER_GRADUAL_DEPOSITOR = "gradual_depositor"
TIER_GRADUAL_DEPOSITOR_WITHDRAWER = "gradual_depositor_withdrawer"
TIER_FREQUENT_DEPOSITOR = "frequent_depositor"
TIER_FREQUENT_DEPOSITOR_WITHDRAWER = "frequent_depositor_withdrawer"

TIER_AND_SEGMENT_BY_FACTS = {
    (False, False): (TIER_GRADUAL_DEPOSITOR, SEGMENT_ACTIVE_DORMANT),
    (False, True): (TIER_GRADUAL_DEPOSITOR_WITHDRAWER, SEGMENT_ACTIVE_DORMANT),
    (True, False): (TIER_FREQUENT_DEPOSITOR, SEGMENT_ACTIVE_ACTIVE),
    (True, True): (TIER_FREQUENT_DEPOSITOR_WITHDRAWER, SEGMENT_ACTIVE_ACTIVE),
}
SEGMENT_BY_TIER = dict(TIER_AND_SEGMENT_BY_FACTS.values())
ACTIVE_TIERS_IN_DISPLAY_ORDER = tuple(SEGMENT_BY_TIER)

_UPSERT_COLUMNS = [
    "purchased_recently",
    "real_withdrawer",
    "tier",
    "segment",
    "computed_at",
    "thresholds_version",
    "run_id",
]
_TIER_IS_CHANGING = "client_tier.tier <> EXCLUDED.tier"
_UPDATE_ON_CONFLICT_CLAUSE = ", ".join(
    [
        f"previous_tier = CASE WHEN {_TIER_IS_CHANGING} "
        "THEN client_tier.tier ELSE client_tier.previous_tier END",
        f"tier_changed_at = CASE WHEN {_TIER_IS_CHANGING} "
        "THEN EXCLUDED.computed_at ELSE client_tier.tier_changed_at END",
        *(f"{column} = EXCLUDED.{column}" for column in _UPSERT_COLUMNS),
    ]
)


@dataclass(frozen=True)
class TierThresholds:
    version: int
    review_period_days: int
    recent_purchase_days: int
    capital_preservation_threshold_kes: float


@dataclass(frozen=True)
class ClientActivity:
    client_id: int
    total_balance_kes: float
    days_since_newest_deposit: int | None
    days_since_newest_real_withdrawal: int | None


@dataclass(frozen=True)
class TierResult:
    purchased_recently: bool
    real_withdrawer: bool
    tier: str
    segment: str | None


def load_tier_thresholds_in_force(session: Session, on_date: date) -> TierThresholds:
    thresholds_row = session.scalars(
        select(ClientTierThresholds)
        .where(
            ClientTierThresholds.valid_from <= on_date,
            or_(ClientTierThresholds.valid_to.is_(None), ClientTierThresholds.valid_to > on_date),
        )
        .order_by(ClientTierThresholds.valid_from.desc(), ClientTierThresholds.version.desc())
        .limit(1)
    ).first()
    if thresholds_row is None:
        raise LookupError(f"no client tier thresholds in force on {on_date}")
    return TierThresholds(
        version=thresholds_row.version,
        review_period_days=thresholds_row.review_period_days,
        recent_purchase_days=thresholds_row.recent_purchase_days,
        capital_preservation_threshold_kes=thresholds_row.capital_preservation_threshold_kes,
    )


def _days_since(day: date | None, reference_date: date) -> int | None:
    return None if day is None else max(0, (reference_date - day).days)


def _days_since_most_recent_event(days_since_each_event: Iterable[int | None]) -> int | None:
    known_days = [days for days in days_since_each_event if days is not None]
    return min(known_days) if known_days else None


def combine_fund_rows_into_client_activity(
    fund_measures: Iterable[ActiveFeatureMeasures], reference_date: date
) -> dict[int, ClientActivity]:
    measures_by_client: dict[int, list[ActiveFeatureMeasures]] = {}
    for fund_measure in fund_measures:
        measures_by_client.setdefault(fund_measure.client_id, []).append(fund_measure)
    return {
        client_id: ClientActivity(
            client_id=client_id,
            total_balance_kes=sum(measure.balance or 0.0 for measure in client_fund_measures),
            days_since_newest_deposit=_days_since_most_recent_event(
                measure.days_since_deposit for measure in client_fund_measures
            ),
            days_since_newest_real_withdrawal=_days_since_most_recent_event(
                _days_since(measure.last_withdrawal_date, reference_date)
                for measure in client_fund_measures
            ),
        )
        for client_id, client_fund_measures in measures_by_client.items()
    }


def _is_within_days(days_since_event: int | None, limit_days: int) -> bool:
    return days_since_event is not None and days_since_event <= limit_days


def classify_client_tier(activity: ClientActivity, thresholds: TierThresholds) -> TierResult:
    purchased_recently = _is_within_days(
        activity.days_since_newest_deposit, thresholds.recent_purchase_days
    )
    real_withdrawer = _is_within_days(
        activity.days_since_newest_real_withdrawal, thresholds.review_period_days
    )
    deposited_in_review_period = _is_within_days(
        activity.days_since_newest_deposit, thresholds.review_period_days
    )
    transacted_in_review_period = deposited_in_review_period or real_withdrawer

    if activity.total_balance_kes < thresholds.capital_preservation_threshold_kes:
        tier, segment = BELOW_CAPITAL_PRESERVATION_THRESHOLD, None
    elif not transacted_in_review_period:
        tier, segment = NO_TRANSACTIONS_IN_REVIEW_PERIOD, None
    else:
        tier, segment = TIER_AND_SEGMENT_BY_FACTS[(purchased_recently, real_withdrawer)]
    return TierResult(purchased_recently, real_withdrawer, tier, segment)


def write_client_tiers(
    session: Session,
    fund_measures: Iterable[ActiveFeatureMeasures],
    *,
    reference_date: date,
    run_id: str,
) -> dict[str, int]:
    thresholds = load_tier_thresholds_in_force(session, reference_date)
    computed_at = datetime.now(UTC)
    tier_rows = []
    for activity in combine_fund_rows_into_client_activity(fund_measures, reference_date).values():
        result = classify_client_tier(activity, thresholds)
        tier_rows.append(
            {
                "client_id": activity.client_id,
                "purchased_recently": result.purchased_recently,
                "real_withdrawer": result.real_withdrawer,
                "tier": result.tier,
                "segment": result.segment,
                "computed_at": computed_at,
                "thresholds_version": thresholds.version,
                "run_id": run_id,
            }
        )

    copy_upsert(
        session, ClientTier.__tablename__, tier_rows, ["client_id"], _UPDATE_ON_CONFLICT_CLAUSE
    )
    clients_per_tier = dict(Counter(row["tier"] for row in tier_rows))
    record_audit(
        session,
        entity_type="client_tier",
        action="recompute",
        entity_id=run_id,
        run_id=run_id,
        detail={
            "clients": len(tier_rows),
            "by_tier": clients_per_tier,
            "thresholds_version": thresholds.version,
        },
    )
    logger.info(
        "client_tier.written",
        clients=len(tier_rows),
        thresholds=thresholds.version,
        **clients_per_tier,
    )
    return clients_per_tier
